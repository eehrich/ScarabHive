# Software Architecture Document: Plugin Architecture

The verified quick reference is the Claude skill `.claude/skills/plugin-authoring/`.

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
- **Tool plugins** - Callable functions for agents (tool servers)
- **Hook plugins** - Lifecycle interception and modification
- **Web plugins** - FastAPI routers / panels
- **Hybrid plugins** - Any combination of tools, hooks and web
- **Agent plugins and config-based agents** - Agent classes and YAML-defined agents
- **LLM provider plugins** - LLM clients (`src/plugins/`)

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
- Runtime plugin loading/unloading. Discovery and instantiation happen at startup; a partial config reload exists (`POST /admin/reload-config`, `agent-cli reload`) that calls `reload_config(new_server_config)` on servers implementing it (`services/config_reload.py`). New servers still need a restart.
- Cross-plugin explicit dependencies (implicit tool deps allowed)
- Plugin marketplace or distribution system

---

## 3. Plugin Types

The `type` list in `plugin.toml` declares what a plugin is: `tool-server`, `hooks`, `web`, `library`, `llm-provider` (`custom` exists but is unused). A plugin may list several.

At runtime, capabilities are detected from the instance and its `schema.yaml`, not from the manifest type:
- **Tools:** the `tools:` section of `schema.yaml`, served by `SchemaBasedToolServer` (`tools/schema_based.py`)
- **Hooks:** the `hooks:` section of `schema.yaml` (`tools/integration.py`)
- **Web:** the instance has `get_web_router` (`plugins/tool_adapter.py`)

Special cases:
- **library** (config-only): only `agents/*.yaml`, prompts, skills; no entrypoint module, not a discoverable type.
- **llm-provider:** `src/plugins/<dir>/provider.py` exporting `PROVIDERS`, loaded by `llm/registry.py`, which requires `type` to contain `llm-provider` and the provider name to be declared in `provides`.
- **Agent plugins:** an `Agent` / `SchemaBasedAgent` subclass exported via `PLUGIN_FACTORY = make_agent_plugin_factory(MyAgent)` (`plugins/factory_utils.py`).

### 3.1 Tool Plugins (SchemaBasedToolServer)

**Purpose:** Provide callable tools for agents

**Base Class:** `SchemaBasedToolServer` (`agent_system.tools.schema_based`)

**Capabilities:**
- Tool definitions live in `schema.yaml` under `tools:` (`{{ name }}` is replaced by the instance name)
- Routing (`tools/schema_mixin.py`): tool `{name}_x` calls method `x`; a tool named exactly `{name}` calls `execute`

**Example:**
```yaml
# schema.yaml
tools:
  - type: function
    function:
      name: "{{ name }}_search"
      description: Search the web
      parameters:
        type: object
        properties:
          query: {type: string}
        required: [query]
```

```python
# server.py
from agent_system.tools.schema_based import SchemaBasedToolServer

class WebSearchServer(SchemaBasedToolServer):
    def __init__(self, name, system_config, server_config):
        super().__init__(name, system_config, server_config)

    async def search(self, params: dict) -> dict:
        return {"results": await self._perform_search(params["query"])}

# plugin.py
PLUGIN_FACTORY = WebSearchServer
```

**Use Cases:**
- Web search, API calls
- Data fetching, file operations
- Calculations, conversions
- External service integration

---

### 3.2 Hook Plugins (SchemaBasedPluginHook)

**Purpose:** Intercept and modify agent lifecycle events

**Base Class:** `SchemaBasedPluginHook` (`agent_system.hooks`), constructed with the plugin directory; its `name` is the directory name. Hooks are declared in `schema.yaml` under `hooks:`; the handler method name equals the hook name.

**Capabilities:**
- Lifecycle event interception
- Message modification
- Context optimization
- Logging and monitoring

**Example** (after `src/plugins/request_logger/`):
```yaml
# schema.yaml
hooks:
  - name: log_pre_llm
    type: pre_llm_call
    enabled: true
    timeout: 2.0
    order:
      after: ["begin"]
  - name: to_markdown
    type: format_output
    enabled: true
```

```python
from pathlib import Path
from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

class RequestLoggerPlugin(SchemaBasedPluginHook):
    def __init__(self, plugin_dir: Path, server_config=None):
        super().__init__(plugin_dir)
        self.request_count = 0

    async def log_pre_llm(self, context: HookContext) -> HookResult:
        self.request_count += 1
        logger.info(f"Request #{self.request_count}")
        return HookResult(success=True, modified=False, context=context)

    async def to_markdown(self, context: HookContext) -> HookResult:
        context.output = f"**Response:**\n\n{context.output}"
        return HookResult(success=True, modified=True, context=context)

# plugin.py
def PLUGIN_FACTORY(name=None, system_config=None, server_config=None):
    return RequestLoggerPlugin(Path(__file__).parent, server_config)
```

**Use Cases:**
- Logging, monitoring
- Context optimization
- Message validation
- Output formatting
- Token tracking

---

### 3.3 Hybrid Plugins (tools + hooks)

**Purpose:** Provide both tools AND lifecycle hooks

**Pattern:** one instance whose `schema.yaml` has both `tools:` and `hooks:`, e.g. `ContextEngineerServer(SchemaBasedToolServer, PluginHook)` in `src/plugins/context_engineer/`. A wrapper object may instead expose the hook implementation as `hooks_plugin` (see `tools/integration.py`).

**Example:**
```python
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.hooks import PluginHook, HookContext, HookResult

class EnhancedSearchServer(SchemaBasedToolServer, PluginHook):
    async def search(self, params: dict) -> dict:          # tool {name}_search
        return {"results": await self._search(params["query"])}

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # adjust context.messages before the LLM call
        return HookResult(success=True, modified=False, context=context)
```

**Note:** `pre_tool_call` / `post_tool_call` fire around every tool call of the model (`ToolExecutionManager.execute_tools_streaming`) and of a tool_script script (`Agent.dispatch_tool_call(hook_source=...)`). A pre hook may change the arguments or block the call (`metadata["block"]`); a post hook may change the result before it joins the history. Contract: `docs/plugin_hooks.md`.

**Use Cases:**
- Tools that need lifecycle awareness (context engineering, sequential thinking)
- Tool usage analytics via LLM hooks

---

### 3.4 Plugin Type Selection Guide

| Scenario | Manifest `type` | Base Class(es) |
|----------|-----------------|----------------|
| Provide callable tools | `tool-server` | `SchemaBasedToolServer` |
| Log/monitor agent, optimize context, format output | `hooks` | `SchemaBasedPluginHook` |
| Tools + hooks | `tool-server`, `hooks` | `SchemaBasedToolServer, PluginHook` |
| HTTP routes / panel | `web` | object with `get_web_router` |
| Agent with custom logic | `tool-server` | `Agent` / `SchemaBasedAgent` + `make_agent_plugin_factory` |
| Agents/prompts/skills only | `library` | none (no entrypoint) |
| LLM client | `llm-provider` | `provider.py` exporting `PROVIDERS` |

**Rule:** Choose the simplest base class that meets your needs. Don't inherit from `ToolServer` if you don't provide tools.

---

## 4. Component Architecture

### 4.1 Plugin System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                      Plugin System                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌───────────────────┐         ┌───────────────────┐           │
│  │  Runtime          │◄────────│ Plugin Discovery  │           │
│  │ (type → factory)  │         │ (dirs + entry pts)│           │
│  └─────────┬─────────┘         └───────────────────┘           │
│            │                                                     │
│            │ instantiate                                         │
│            ▼                                                     │
│  ┌───────────────────────────────────────────────┐             │
│  │           Plugin Instances                     │             │
│  ├───────────────────────────────────────────────┤             │
│  │  Tools (schema) │  Hooks (schema) │  Web      │             │
│  └───────────────────────────────────────────────┘             │
│            │                    │                                │
└────────────┼────────────────────┼────────────────────────────────┘
             │                    │
             ▼                    ▼
┌────────────────────┐   ┌────────────────────┐
│   ToolServerRegistry /    │   │   HookRegistry     │
│ PluginToolRegistry  │   │ (lifecycle events) │
└────────────────────┘   └────────────────────┘
```

### 4.2 Core Components

#### 4.2.1 Runtime

**File:** `src/agent_system/runtime.py` (`class Runtime`)

There is no separate plugin registry class. Discovery returns a plain dict `type name → factory`; `Runtime` turns the `plugins.servers` config into declarations and instantiates them.

**Responsibilities:**
- Call `discover_all_plugins(dirs=config.plugins.plugin_dirs)`
- Build one declaration per configured server (`type` selects the factory)
- Construct instances: `factory(name, system_config, server_config)`, plus `registry=` when the factory has `_accepts_registry` (agent factories from `make_agent_plugin_factory`)
- Register instances in `ToolServerRegistry` and `PluginToolRegistry` (`plugins/tool_adapter.py`, which also registers web routers)

#### 4.2.2 Plugin Discovery

**File:** `src/agent_system/plugins/discovery.py`

**Responsibilities:**
- Scan every immediate subdirectory of each plugin dir (`plugins.plugin_dirs`: `src/plugins`, `src/plugins_writer`, `src/plugins_trading`) plus the `agent_system.tool_plugins` entry point group
- A plugin dir's name is its package name (`plugins.<name>`). Two plugin dirs with the same name (`src/plugins`, `external/plugins`) share it: a plugin or shared module the first one has is not loaded from the second -- whether the first one's loads or not -- and the second logs a warning: both would be the same module
- Read `plugin.toml` (`plugins/plugin_manifest.py`)
- Import the entrypoint module and fetch the factory
- Register hooks declared in plugin schemas (`register_plugin_hooks`)

**Discovery Process (simplified):**
```python
def discover_plugins(path: Path) -> dict[str, Callable]:
    out = {}
    for d in path.iterdir():
        metadata = load_plugin_metadata(d)            # plugin.toml [plugin]
        module, factory_name = metadata.get(
            "entrypoint", "plugin:PLUGIN_FACTORY").split(":", 1)
        if not (d / f"{module}.py").exists():
            continue                                  # e.g. library plugins
        mod = import_module_from(d / f"{module}.py")
        factory = getattr(mod, factory_name, None) or getattr(mod, "PLUGIN_FACTORY", None)
        if factory:
            out[d.name] = factory                     # type name = folder name
    return out
```

Agents defined in YAML are not discovered here: they are ordinary `plugins.servers` entries (see §7).

#### 4.2.3 Hook Registry

**Files:** `src/agent_system/hooks/registry.py` (`HookRegistry`), `src/agent_system/servers/agent/components/hook_integration.py` (`HookIntegrationManager`, the per-agent entry points such as `execute_pre_llm_hooks`)

**Responsibilities:**
- Hook registration from plugin schemas
- Execution order (topological sort over `order`)
- Error isolation and per-hook timeouts
- Per-agent enable/disable and `hook_config` overrides

**Hook Lifecycle (simplified `HookRegistry.execute_hooks`):**
```python
async def execute_hooks(self, hook_type, context, timeout=None, hook_filter=None):
    ordered = self._topological_sort(self._hooks[hook_type])  # cycle → logged, registration order
    current = context
    for name, hook, meta in ordered:
        try:
            result = await asyncio.wait_for(
                self._execute_hook_method(hook_type, hook, self._deep_copy_context(current)),
                meta.get("timeout", timeout or self.default_timeout))
            if result.success:
                if result.modified and result.context:
                    current = result.context
                if result.metadata:
                    current.metadata.update(result.metadata)
        except Exception as e:
            logger.error(f"Hook '{name}' raised exception: {e}")  # continue (isolation)
    return current
```

---

## 5. Plugin Discovery

### 5.1 Filesystem-Based Discovery

**Structure:**
```
src/plugins/                 # also src/plugins_writer/, src/plugins_trading/
├── datetime/
│   ├── plugin.toml        # Manifest ([plugin] table)
│   ├── plugin.py          # Entrypoint module: PLUGIN_FACTORY
│   ├── server.py          # Implementation
│   ├── schema.yaml        # tools: / hooks: / config: / web_ui:
│   └── tests/
└── request_logger/
    ├── plugin.toml
    ├── plugin.py
    ├── hooks.py
    └── schema.yaml
```

The plugin type name is the **folder name**; the manifest `name` renames nothing. The entrypoint is `module:Factory` with the module file directly in the plugin folder (default `plugin:PLUGIN_FACTORY`). A folder without that module (e.g. `library` plugins) is not a discoverable type. `plugin.yaml` is not read.

**plugin.toml Example:**
```toml
[plugin]
name = "request_logger"
version = "1.0.0"
description = "Example hook plugin that logs agent requests and responses"
entrypoint = "plugin:PLUGIN_FACTORY"
type = ["hooks"]            # tool-server, hooks, web, library, llm-provider
category = "monitoring"
tags = ["hooks", "logging"]
requires = { agent_system = ">=0.6.0" }
# dependencies = ["somepkg>=1.0"]   # pip requirements, aggregated into the build
```

Tools, hooks and config defaults are declared in `schema.yaml` (see §3.1, §3.2), not in the manifest.

### 5.2 Configuration-Based Discovery

**Files:** any YAML pulled in by the `includes` of `config/config.yaml`, notably `config/agents*/*.yaml` and `src/plugins*/*/agents/*.yaml`

```yaml
plugins:
  servers:
    researcher:
      type: basic_agent          # a discovered plugin type
      enabled: true
      description: "Research assistant"
      agent_config:
        llm_profile: [or-deepseek-flash, gemini-flash]   # fallback chain
        max_steps: 10
        tools:
          allowed:
            - "datetime/*"
            - "context_engineer/context_engineer_read"
        hooks:
          enabled: true
          overrides:
            context_engineer.engineer_context:
              enabled: true
```

**How It Works:**
1. The config loader merges all included files into one `plugins.servers` map
2. Each entry's `type` names a discovered plugin type (or another configured server to inherit from)
3. `Runtime` constructs the instance through that type's factory with the entry as `server_config`
4. A sub-agent must additionally be registered in the sub-agent manager (SAM) to be spawnable

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
| `pre_llm_request` | LLM client, before the HTTP request | Capture the exact API payload |
| `post_llm_response` | LLM client, after the HTTP response | Capture raw response, usage, timing |
| `llm_progress` | During a streaming call, every few KB of thinking | Progress/monitoring (no messages) |
| `format_output` | Before returning to user | Format `output` (MD, HTML, etc.) |
| `session_start` | Session begins | Initialize session state |
| `session_end` | Session ends | Cleanup, save state |
| `pre_tool_call` | Before each tool call of the model / a tool_script script | Change arguments, block the call |
| `post_tool_call` | After the call ran, before its result joins the history | Change the result |

The enum is `HookType` in `hooks/plugin_hook.py`.

### 6.2 Hook Context

**Data Structure** (`hooks/plugin_hook.py`, abridged):
```python
@dataclass
class HookContext:
    hook_type: HookType
    request_id: str
    session_id: str
    agent: Optional[Agent] = None
    agent_name: str = ""
    messages: Optional[List[ChatMessage]] = None
    tools_schema: Optional[List[Dict[str, Any]]] = None
    llm_response: Optional[Dict[str, Any]] = None
    tool_call: Optional[Dict[str, Any]] = None
    tool_result: Optional[Dict[str, Any]] = None
    output: Optional[str] = None          # format_output
    output_format: str = "text"           # 'html', 'ansi', 'text', 'markdown'
    metadata: Dict[str, Any] = field(default_factory=dict)
    hook_config: Dict[str, Any] = field(default_factory=dict)  # per-agent hooks.overrides
    target_hook_name: Optional[str] = None
    step: int = 0
    llm: Optional[Any] = None
    cancellation_token: Optional[Any] = None
    # pre_llm_request / post_llm_response
    llm_request_payload, llm_response_data, llm_provider, llm_model,
    llm_request_url, llm_duration_ms, llm_error, llm_usage,
    llm_finish_reason, llm_is_streaming
    # llm_progress
    reasoning_text, reasoning_chars, previous_reasoning_chars
```

Each hook receives a deep copy of the context.

### 6.3 Hook Result

```python
@dataclass
class HookResult:
    """Result from hook execution"""
    success: bool
    modified: bool = False  # Whether context was modified
    context: Optional[HookContext] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)  # merged into context.metadata
```

### 6.4 Hook Ordering

**Directives:**
- `after: [...]` - Execute after these
- `before: [...]` - Execute before these

A reference resolves only to a **full hook name** `<instance>.<hook>` (e.g. `context_engineer.engineer_context`), a **category** (the hook's `category` field), or the virtual nodes `begin` / `end`. A short hook name matches nothing and is silently ignored (debug log).

**Example:**
```yaml
hooks:
  - name: token_counter
    type: pre_llm_call
    order:
      after: ["begin"]
      before: ["context_engineering"]        # category

  - name: summarize
    type: pre_llm_call
    category: context_summarization
    order:
      after: ["context_engineer.engineer_context"]   # full name
      before: ["end"]
```

**Resolution:**
Topological sort (`HookRegistry._topological_sort`). On a cycle the error is logged and hooks run in registration order.

---

## 7. Configuration-Based Agents

### 7.1 Architecture

Config-based agents are ordinary `plugins.servers` entries whose `type` is an agent plugin (usually `basic_agent`) or another configured server they inherit from. No special factory exists: `Runtime` calls the agent plugin's factory (from `make_agent_plugin_factory`) with the entry as `server_config`.

```yaml
# config/agents/translator.yaml (or src/plugins/<plugin>/agents/translator.yaml)
plugins:
  servers:
    translator:
      type: basic_agent
      enabled: true
      description: "Translation agent"
      agent_config:
        llm_profile: [gemini-flash]
        max_steps: 5
        system_template: prompts/translator.md   # file path; inline text goes in system_prompt
        tools:
          allowed: []
```

`ToolServerConfig` is a pydantic model with `extra=allow`; it has no `.get()`. Plugin code reads fields via attributes / `getattr(server_config, "field", default)`.

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
| Tool access only by allowlist | `tools.allowed` with `server/tool` or `server/*` patterns |
| No custom hooks | Write a hook plugin; toggle per agent via `hooks.overrides` |

---

## 8. Key Design Decisions

### ADR-001: Composition Over Inheritance

**Context:** Need flexible plugin types
**Decision:** Independent `ToolServer` and `PluginHook` base classes
**Rationale:**
- Hook-only plugins don't need tool methods
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
**Decision:** Scan the `plugins.plugin_dirs` (plus entry points) at startup
**Rationale:**
- Developer-friendly (drop in folder)
- No registration boilerplate
- plugin.toml for metadata
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
Config loaded (config.yaml + includes → plugins.servers)
    │
    ▼
Runtime(config)  →  discover_all_plugins(dirs=plugins.plugin_dirs)
    │
    ├─► For each subdirectory of each plugin dir (+ entry points)
    │   │
    │   ├─► Load plugin.toml (metadata)
    │   ├─► Import entrypoint module (default plugin.py)
    │   ├─► Fetch factory (default PLUGIN_FACTORY)
    │   ├─► dict[folder name → factory]
    │   │
    ▼
One declaration per plugins.servers entry (type → factory)
    │
    ▼
Instantiate servers
    │
    ├─► factory(name, system_config, server_config[, registry])
    ├─► Register in ToolServerRegistry / PluginToolRegistry (tools, web)
    ├─► Register schema hooks in HookRegistry
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
    ├─► HookIntegrationManager.execute_pre_llm_hooks
    │   └─► HookRegistry.execute_hooks(PRE_LLM_CALL, context)
    │   │
    │   ├─► Get all pre_llm_call hooks
    │   ├─► Sort by order (topological sort)
    │   ├─► For each enabled hook (deep-copied context):
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
    ├─► HookRegistry.execute_hooks(POST_LLM_CALL, context)
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
- ✅ Use minimal base class (don't inherit `ToolServer` if you don't provide tools)
- ✅ Validate inputs with Pydantic models
- ✅ Handle errors gracefully (return error results, don't raise)
- ✅ Define tools in `schema.yaml`, metadata in `plugin.toml`
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
- ❌ Modify context without setting `modified=True` (the changes are discarded; only `HookResult.metadata` is still merged)
- ❌ Raise exceptions (system catches but wastes resources)
- ❌ Have side effects beyond context modification

### 10.3 Configuration

**DO:**
- ✅ Use config-based agents for simple cases
- ✅ Provide good defaults in plugin config
- ✅ Validate config with Pydantic models
- ✅ Document config options in the `config:` section of `schema.yaml`
- ✅ Read `ToolServerConfig` fields with `getattr` (pydantic model, no `.get()`)

**DON'T:**
- ❌ Hardcode values (use config)
- ❌ Require complex config for simple plugins
- ❌ Change config structure without migration plan

---

## 11. Related Documents

### 11.1 Architecture Documents

- [System Architecture](_arch_agent_system_architecture.md) - Overall system
- [App Architecture](_arch_app_architecture.md) - FastAPI application
- [CLI Architecture](_arch_cli_architecture.md) - Command-line interface
- Quick reference: `.claude/skills/plugin-authoring/`

### 11.2 Development Guides

- [Plugin Authoring](plugin_authoring.md) - How to create plugins
- [Plugin Hooks](plugin_hooks.md) - Hook system details
- [Configurable Agents](configurable_agents.md) - YAML-based agents

### 11.3 Design Documents

- [Hook System Design](plugin_hooks.md) - Lifecycle hooks
- [Tool Execution](tool_execution.md) - Tool discovery and execution
- [Tool server configuration](server_configuration.md) - Server configuration

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
