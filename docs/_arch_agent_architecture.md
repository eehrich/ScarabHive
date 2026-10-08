# Agent Architecture

## Overview

The Agent System provides two base classes for building intelligent agents with MCP (Model Context Protocol) integration:

1. **`Agent`** - Core agent logic for LLM interaction, tool orchestration, and conversation management
2. **`SchemaBasedAgent`** - Extends Agent with automatic `schema.yaml` loading for declarative tool definitions

This architecture follows the **Single Responsibility Principle** and maintains consistency with the existing `SchemaBasedToolServer` pattern used for simpler tools.

## Architecture Diagram

```
ToolServer (base protocol implementation)
│
├─ SchemaBasedToolServer (simple schema-based tools)
│  ├─ DateTimeServer
│  ├─ WebScraperServer
│  ├─ LLMRouterServer
│  └─ ... (other schema-based tools)
│
└─ Agent (core agent logic)
   │
   └─ SchemaBasedAgent (adds schema.yaml loading)
      ├─ BasicAgent
      └─ ... (custom intelligent agents)
```

## Base Classes

### `Agent` - Core Agent Logic

**Location:** `src/agent_system/servers/agent/server.py`

**Purpose:** Provides core functionality for intelligent agents:
- LLM integration (conversation management, streaming)
- Tool orchestration (execution, result handling)
- Hook integration (pre/post message, context optimization)
- Session management (message history, context)
- Error handling and retry logic
- **LLM fallback system** with automatic recovery for rate limits and quota exhaustion

**Use When:**
- Building custom agents with **programmatic tool definitions**
- Tools are dynamic or require runtime configuration
- Complex tool generation logic beyond simple schema files

**Example:**
```python
from agent_system.servers.agent import Agent
from typing import Any, Dict

class CustomAgent(Agent):
    """Agent with programmatically defined tools."""
    
    def get_tools(self) -> list[Dict[str, Any]]:
        """Define tools programmatically."""
        return [
            {
                "name": "custom_tool",
                "description": f"A tool for {self.name}",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "What the agent should do"
                        }
                    },
                    "required": ["task"]
                }
            }
        ]

    # No handler method: calling any of the agent's tools runs the agent
    # (Agent.call takes the task from `task`, `query` or `prompt`).
```

### `SchemaBasedAgent` - Schema-Based Agent

**Location:** `src/agent_system/servers/agent/schema_based.py`

**Purpose:** Extends Agent with automatic `schema.yaml` loading:
- Loads tool definitions from `schema.yaml` in plugin directory
- Supports Jinja2 template variables (`{{name}}`, etc.)
- Caches loaded schemas for performance
- Follows declarative configuration pattern

**Use When:**
- Building agents with **static, declarative tool definitions**
- Tools don't require runtime generation
- Maintaining consistency with other schema-based plugins
- Simplifying plugin maintenance (tools in YAML, not code)

**Example:**
```python
from agent_system.servers.agent.schema_based import SchemaBasedAgent

class MyAgent(SchemaBasedAgent):
    """Agent with schema.yaml tool definitions."""
    
    # No need to override get_tools() - automatically loaded
    
    async def my_tool(self, arguments: dict) -> str:
        """Handle tool defined in schema.yaml."""
        return f"Result: {arguments['input']}"
```

**Corresponding `schema.yaml`:**
```yaml
tools:
  - name: my_tool
    description: A declarative tool for {{name}}
    inputSchema:
      type: object
      properties:
        input:
          type: string
          description: Tool input
      required: [input]
```

## Pattern Consistency

### SchemaBasedToolServer vs SchemaBasedAgent

Both follow the same pattern but serve different purposes:

| Feature | SchemaBasedToolServer | SchemaBasedAgent |
|---------|---------------------|------------------|
| **Purpose** | Simple tools | Intelligent agents with LLM |
| **Base Class** | `ToolServer` | `Agent` (extends `ToolServer`) |
| **Schema Loading** | `schema.yaml` | `schema.yaml` |
| **Tool Execution** | Direct handlers | Via Agent orchestration |
| **LLM Integration** | No | Yes (core feature) |
| **Use Case** | Utility tools (datetime, scraper) | Agentic workflows (research, coding) |

**Example Comparison:**

```python
# SchemaBasedToolServer - Simple tool
class DateTimeServer(SchemaBasedToolServer):
    async def get_current_time(self, arguments: dict) -> str:
        return datetime.now().isoformat()

# SchemaBasedAgent - Intelligent agent
class ResearchAgent(SchemaBasedAgent):
    async def research(self, arguments: dict) -> str:
        # Agent can use LLM, other tools, hooks, etc.
        context = await self.llm.analyze(arguments['topic'])
        return await self.synthesize_results(context)
```

## When to Use Each Base Class

### Use `Agent` (Core) When:

✅ Tools require **runtime configuration** or dynamic generation  
✅ Complex tool definition logic beyond simple schemas  
✅ Tools depend on agent-specific state or configuration  
✅ Need full control over tool metadata generation  

**Example Scenarios:**
- Tools with dynamically computed descriptions
- Tools that change based on agent configuration
- Tools requiring complex validation logic
- Integration with external tool registries

### Use `SchemaBasedAgent` When:

✅ Tools can be defined **declaratively in YAML**  
✅ Tools are static and don't require runtime generation  
✅ Want to maintain consistency with schema-based plugins  
✅ Simplifying maintenance (non-developers can edit YAML)  

**Example Scenarios:**
- Standard agent tools (research, scraping, analysis)
- Plugins where tools don't change at runtime
- Teams with non-technical contributors editing tools
- Following established plugin patterns

## Implementation Details

### Schema Loading (SchemaBasedToolMixin)

Both `SchemaBasedToolServer` and `SchemaBasedAgent` inherit from `SchemaBasedToolMixin` (built on `SchemaBaseMixin` in `src/agent_system/core/schema_base_mixin.py`), which provides common schema loading functionality:

**Location:** `src/agent_system/tools/schema_mixin.py`

**Features:**
1. **Directory Discovery:** Uses `importlib.util.find_spec()` for robust plugin directory resolution
2. **Schema Loading:** Calls `load_schema_from_dir()` from `plugins.schema_loader`
3. **Template Variables:** Supports `{{name}}` and custom variables via `get_template_vars()`
4. **Caching:** Stores both full schema (`_schema_cache`) and tools (`_tools_cache`)
5. **Generic Call Dispatcher:** Automatically routes tool calls to methods by name
6. **Development Tools:** `clear_schema_cache()`, `get_schema_data()`

**Performance Characteristics:**
- First call: ~10-50ms (file I/O, YAML parsing)
- Subsequent calls: <1ms (cached)
- Template rendering: <1ms per variable

**Architecture:**
```python
# Mixin provides shared functionality
class SchemaBasedToolMixin(SchemaBaseMixin):
    def _init_schema_mixin()        # Initialize caches
    def _get_plugin_directory()      # Robust directory resolution
    def get_template_vars()          # Template variables (overridable)
    def _load_schema()               # Load and cache schema
    def get_tools()                  # Extract tools from schema
    def get_schema_data()            # Access full schema
    def clear_schema_cache()         # Development utility
    def _get_method_name(tool_name)  # Tool → method routing (overridable)
    async def call(tool, params)     # Generic dispatcher

# Used by both:
class SchemaBasedToolServer(SchemaBasedToolMixin, ToolServer):
    pass  # Direct tool name → method mapping

class SchemaBasedAgent(SchemaBasedToolMixin, Agent):
    def _get_method_name(tool_name):
        # Strip agent name prefix: "basic_agent_execute_task" → "execute_task"
        return tool_name.removeprefix(f"{self.name}_")
```

### Automatic Method Routing

The `SchemaBasedToolMixin.call()` dispatcher automatically routes tool calls to methods:

**For SchemaBasedToolServer:**
- Tool: `"search_tweets"` → Method: `search_tweets(params)`
- Direct 1:1 mapping

**For SchemaBasedAgent:**
- Tool: `"basic_agent_execute_task"` → Method: `execute_task(params)`
- Agent name prefix automatically stripped via `_get_method_name()`

**Example:**
```python
# schema.yaml defines tool
tools:
  - name: "{{name}}_execute_task"
    # ...

# Plugin implements method (no "handle_" prefix needed with SchemaBasedToolMixin)
class BasicAgent(SchemaBasedAgent):
    async def execute_task(self, params: dict) -> dict:
        # Automatically called when tool is invoked
        return {"status": "success"}
```

**No Manual Routing Required:**
```python
# ❌ OLD WAY (manual routing - 40 lines of boilerplate)
async def call(self, tool: str, params: dict):
    if tool == f"{self.name}_execute_task":
        return await self.execute_task(params)
    elif tool == f"{self.name}_list_tools":
        return await self.list_tools(params)
    else:
        raise ValueError(f"Unknown tool: {tool}")

# ✅ NEW WAY (automatic routing - 0 boilerplate)
# Just implement the method - SchemaBasedToolMixin.call() handles routing
async def execute_task(self, params: dict):
    return {"status": "success"}
```

## Migration Guide

### Moving from Agent to SchemaBasedAgent

If you have an agent that loads `schema.yaml` manually:

**Before:**
```python
from agent_system.servers.agent import Agent
from agent_system.plugins.schema_loader import load_schema_from_dir
from pathlib import Path
import inspect

class MyAgent(Agent):
    def get_tools(self) -> list[Dict[str, Any]]:
        plugin_dir = Path(inspect.getfile(self.__class__)).parent
        schema = load_schema_from_dir(str(plugin_dir), {"name": self.name})
        return schema.get("tools", [])
```

**After:**
```python
from agent_system.servers.agent.schema_based import SchemaBasedAgent

class MyAgent(SchemaBasedAgent):
    # get_tools() automatically loads schema.yaml
    pass
```

### Moving from SchemaBasedAgent to Agent

If your tools require runtime generation:

**Before:**
```python
from agent_system.servers.agent.schema_based import SchemaBasedAgent

class MyAgent(SchemaBasedAgent):
    pass  # Uses schema.yaml
```

**After:**
```python
from agent_system.servers.agent import Agent

class MyAgent(Agent):
    def get_tools(self) -> list[Dict[str, Any]]:
        # Generate tools programmatically
        tools = []
        for feature in self.config.get("features", []):
            tools.append({
                "name": f"handle_{feature}",
                "description": f"Dynamic tool for {feature}",
                # ...
            })
        return tools
```

## Plugin Configuration

Both agent types use the same plugin configuration:

**`plugin.toml`:**
```toml
[plugin]
name = "my_agent"
version = "1.0.0"
description = "My custom agent"
author = "Your Name"
entrypoint = "plugin:PLUGIN_FACTORY"
type = ["tool-server"]
```

**Plugin configuration in `plugins:` section:**
```yaml
plugins:
  servers:
    research_agent_1:
      type: my_agent
      enabled: true
      model: "gpt-4"
      max_tokens: 2000
```

## Testing

### Testing Agents

Both agent types should be tested similarly:

```python
import pytest
from agent_system.config.models import AgentSystemConfig, ToolServerConfig

@pytest.fixture
def agent(mock_system_config: AgentSystemConfig):
    """Create agent instance for testing."""
    server_config = ToolServerConfig(
        type="my_agent",
        enabled=True,
        config={}
    )
    agent = MyAgent(
        name="test_agent",
        system_config=mock_system_config,
        server_config=server_config,
        registry=None
    )
    return agent

def test_agent_tools(agent: MyAgent):
    """Test agent returns expected tools."""
    tools = agent.get_tools()
    assert len(tools) > 0
    assert tools[0]["name"] == "expected_tool"
```

### Testing Schema Loading

For `SchemaBasedAgent`, verify schema loading:

```python
def test_schema_based_agent_loads_schema(agent: MyAgent):
    """Test schema.yaml is loaded correctly."""
    tools = agent.get_tools()
    assert len(tools) > 0
    
    # First call loads from file
    tools_1 = agent.get_tools()
    
    # Second call uses cache
    tools_2 = agent.get_tools()
    
    assert tools_1 == tools_2

def test_schema_template_variables(agent: MyAgent):
    """Test template variables are replaced."""
    tools = agent.get_tools()
    tool = tools[0]
    
    # Check {{name}} was replaced with agent name
    assert "{{name}}" not in tool["description"]
    assert agent.name in tool["description"]
```

## Best Practices

### 1. Prefer SchemaBasedAgent for Static Tools

Most agents should use `SchemaBasedAgent` for simplicity and consistency:

✅ **DO:**
```python
class ResearchAgent(SchemaBasedAgent):
    """Uses schema.yaml for tool definitions."""
    pass
```

❌ **DON'T:**
```python
class ResearchAgent(Agent):
    def get_tools(self):
        # Manually loading schema.yaml
        return load_schema_from_dir(...)
```

### 2. Document Custom get_tools() Logic

If using `Agent` with custom tool generation, document why:

```python
class DynamicAgent(Agent):
    """Agent with runtime-generated tools.
    
    Uses Agent base class instead of SchemaBasedAgent because:
    - Tools depend on runtime configuration
    - Tool descriptions include dynamic content
    - Tools are generated from external API metadata
    """
    
    def get_tools(self) -> list[Dict[str, Any]]:
        # Complex tool generation...
        pass
```

### 3. Keep schema.yaml Simple

For `SchemaBasedAgent`, keep `schema.yaml` declarative:

✅ **DO:**
```yaml
tools:
  - name: research
    description: Research a topic using {{name}}
    inputSchema:
      type: object
      properties:
        topic:
          type: string
```

❌ **DON'T:**
```yaml
tools:
  # Avoid complex logic that should be in code
  - name: "{{conditional_tool_name}}"  # Runtime conditionals
    description: "{{complex_computed_description}}"  # Complex computations
```

### 4. Test Both Tool Loading and Execution

```python
def test_agent_full_workflow(agent: MyAgent):
    """Test complete agent workflow."""
    # 1. Verify tools load
    tools = agent.get_tools()
    assert len(tools) > 0
    
    # 2. Verify handler exists
    assert hasattr(agent, "my_tool")
    
    # 3. Test execution
    result = await agent.my_tool({"input": "test"})
    assert result is not None
```

## Troubleshooting

### Schema Not Loading

**Problem:** `SchemaBasedAgent.get_tools()` returns empty list

**Solutions:**
1. Verify `schema.yaml` exists in plugin directory
2. Check YAML syntax is valid (use YAML validator)
3. Check logs for schema loading errors
4. Verify plugin directory structure:
   ```
   src/plugins/my_agent/
   ├── server.py
   ├── schema.yaml  ← Must exist
   └── plugin.toml
   ```

### Template Variables Not Replaced

**Problem:** Tool descriptions contain `{{name}}` literally

**Solutions:**
1. Verify `schema_loader.load_schema_from_dir()` receives template context
2. Check template variables are properly formatted: `{{variable}}` not `{variable}`
3. Verify available variables: `name`, `description`, etc.

### Tools Not Found by LLM

**Problem:** LLM claims tool doesn't exist, but `get_tools()` returns it

**Solutions:**
1. Verify tool handler method exists: the tool name without the `{name}_` prefix (`_get_method_name()`)
2. Check method signature: `async def my_tool(self, arguments: dict) -> str`
3. Verify tool name matches exactly (case-sensitive)
4. Check agent is registered in plugin system

## Summary

The Agent System's two-tier architecture provides flexibility while maintaining simplicity:

- **`Agent`** → Full control, programmatic tools, complex logic
- **`SchemaBasedAgent`** → Declarative, YAML-based, simple maintenance

Choose based on your needs:
- **90% of agents** → Use `SchemaBasedAgent` (declarative, simple)
- **10% of agents** → Use `Agent` (dynamic tools, complex logic)

Both integrate seamlessly with the Agent System's LLM, hooks, and plugin infrastructure.

---

## LLM Fallback and Blocks

If an LLM fails, the agent continues on the next profile in its chain.
Whether an LLM is **blocked** belongs to the LLM, not to the agent: the
block applies to every agent in the process (`src/agent_system/llm/model_health.py`).

### Configuration

Fallbacks are given as a **chain** directly in `llm_profile` (since 2026-07:
list = `[primary, fallback1, fallback2, ...]`; the removed key
`llm_profile_fallbacks` aborts config loading):

```yaml
my_agent:
  type: basic_agent
  agent_config:
    llm_profile: ["gemini", "openai", "anthropic"]   # primary + fallback chain
    llm_profile_advanced: ["gpt-large", "claude"]    # optional: advanced chain (use_advanced_model / auto-escalation)
    fallback_recovery_seconds: 1800                  # longest block this agent sets (default: 3600)
```

Full chain semantics (advanced chain, `llm_params`, migration script
`scripts/migrate_llm_profiles.py`): `docs/basic_agent_llm_profiles.md`.

### What Triggers a Block, and What Does Not

| Error | Block on the LLM (for all agents) | This request |
|---|---|---|
| `LLMRateLimitError` (429) | 60 s, doubled on each further failure up to `fallback_recovery_seconds`; the provider's `retry_after` is the lower bound | next profile in the chain |
| `LLMQuotaExhaustedError` | immediately `fallback_recovery_seconds` | next profile in the chain |
| HTTP 401/402/403/404 | immediately `fallback_recovery_seconds` | next profile; if the base had failed, it becomes the base of the run |
| 5xx, connection errors, any other 4xx (400/408/409/413/422 …) | none | next profile; if the base had failed, it becomes the base of the run |
| Error in the response body (HTTP 200 with `error`) | none | first **the same model once** (per model and step; a gateway hiccup rarely hits twice), then the next profile; a content filter (`content_filter`, `content_filter_<native>`) switches immediately, because the same model would block the same text again, as does an error its client marks with `retried` (it has already retried with backoff itself). If the base had failed, the profile becomes the base of the run; once the chain is exhausted, the run ends with an `error` event |
| no local file descriptors left (EMFILE) | none | no switch, error |

**A burst is one failure.** A 429 on a call that started before the block
was set does not double it: seven requests in flight when the minute window
closes block for 60 s, not for an hour. If such a straggler arrives only after
the block has expired (the clients retry a 429 themselves before reporting
it), it blocks nothing at all — otherwise it would ruin a probe that the LLM
has just passed as healthy. A `fallback_recovery_seconds` of 0 blocks nothing. **No block shortens a longer one that is still running** — an agent with a
short `fallback_recovery_seconds` does not cut another agent's quota block
down to its own.

The key of an LLM is the client's **(endpoint, key, model)**:
- The endpoint is the `base_url`; a client without one (SDK clients, batch
  client) is its own endpoint, named after its class — a batch quota is not
  the sync quota of the same model.
- The key is a fingerprint of the API key (never the key itself): a rejected
  or exhausted key says nothing about another one.
- Two profiles with the same model at the same URL with the same key are
  one LLM, regardless of which client class they use. Clients without their
  own URL (SDK, batch) are one endpoint per class, regardless of which server
  they reach.

Provider routing is not part of it — a 429 from an OpenRouter backend
also blocks the model for a profile with different routing.

### How a Step Chooses Its LLM

Before each step, **before** the pre-LLM hooks:

1. If the escalation window is open and the advanced LLM is free, the
   step runs on it. If it is blocked, this step counts as not escalated
   (the window stays open) and the procedure continues with 2.
2. The desired LLM is then the request's override (`--llm`, selection in the
   chat), otherwise the base of the run. If that LLM is free, the step runs on
   it.
3. Otherwise it runs on the first free profile in the chain.
4. If none is free, it runs on the desired one anyway — a blocked LLM
   is better than none.

If the call fails, the step tries the remaining profiles in the chain —
free ones first, then blocked ones, each once. The base of the run is included
if the step did not run on it: as the **first** member after a failed
escalation (the advanced model says nothing about the base), as the **last**
after a failed detour around a blocked base. This way the run does not abort
as long as an LLM is left.

An explicit choice is **not** an exception: a blocked LLM stays
blocked, even if it is chosen in the chat. Another, free LLM, on the other
hand, runs immediately — one LLM's block does not hold it up.

If the hooks take long while a block is set or lifted, the step chooses again
afterwards (`model_health.version`). Every model switch, including the switch
back, removes the previous model's reasoning artifacts from the history.

### Lifting a Block

- **A response lifts the block for everyone** — provided the call started after
  the block was set. A response to an older call says nothing about the LLM
  afterwards.
- **After expiry** exactly one request probes the LLM; the others keep
  treating it as blocked until the probe answers (lift) or fails with a
  blocking error (new block: twice as long for a 429, `fallback_recovery_seconds`
  again for quota or a rejected key). If the probe ends without a verdict (5xx,
  abort), the LLM is free for the next request after 120 s;
  asking again from the same request does not extend this period (after
it expires the probe is reassigned to whoever asks first). If the request picks a different LLM after the
  hooks, it returns the probe immediately. This keeps twenty agents from
  running into the same 429 at once.

### Limits

- **Per process.** The job workers of a further plugin root are separate
  processes with their own blocks; what the API blocks does not reach them.
- **All members tried and the call fails:** the error of the last attempt goes
  to the caller (for an error in the response body: an `error` event).

### Status Messages

```json
{
  "type": "progress",
  "message": "Rate limit hit, switching to openai, retry in 60s",
  "meta": {"step": 3, "fallback": "openai", "blocked_seconds": 60.0}
}
```

A step that runs around a blocked LLM reports itself as
`Calling LLM (openai:fallback)`. In the log:

- `LLM <model> blocked for 60s for every agent (rate limit hit, seen by <agent>)`
- `[<agent>] LLM <model> is blocked for 42s more; this step runs on openai`
- `LLM <model> answers again: unblocked for every agent`

---

## Internal Component Architecture (Oct 2025 Refactoring)

The `Agent` class has been refactored into a modular component-based architecture for better maintainability and testability. This section documents the internal components (for developers working on the agent system itself).

### Component Structure

```
src/agent_system/servers/agent/
├── server.py (core agent orchestration: request lifecycle, LLM loop, fallback chain)
├── schema_based.py (SchemaBasedAgent - schema.yaml tool loading)
├── components/
│   ├── session_tracking.py (session & message management, session locks, compaction marker)
│   ├── request_manager.py (request lifecycle & cancellation)
│   ├── hook_integration.py (hook execution at all lifecycle points, LLM transport hooks)
│   ├── tool_integration.py (MCP protocol handling, external tool schemas)
│   ├── tool_execution.py (tool call execution: parallel, cancellable, streaming)
│   ├── server_resolution.py (shared server/tool-name resolution building blocks)
│   └── status_forwarding.py (status event streaming)
├── prompt_strategies.py (prompt rendering, strategy pattern)
├── tool_discovery.py (tool enumeration & discovery-stage filtering)
├── tool_schema_builder.py (tool schema generation + THE shared pattern matchers)
├── loop_detection.py (tool-call loop detection, per-request)
├── escalation.py (stuck-triggered auto-escalation to advanced profile)
└── result_utils.py (result extraction & formatting)
```

### Core Components

#### 1. SessionTracker (`session_tracking.py`)
**Purpose:** Manages session lifecycle and message history

**Key Methods:**
- `append_user_message(request_id, content)` - Append message to active request
- `append_to_session(session_id, content)` - Append to persisted session
- `drain_appended_messages(request_id, messages)` - Consume pending messages
- `get/set_session_messages(session_id)` - Session history access
- `has_session(session_id)` - Check session existence
- `delete_session(session_id)` - Remove session
- `get_all_session_ids()` - List all sessions

**Manages:**
- `_sessions: Dict[str, List[ChatMessage]]` - Session message history
- `_request_to_session: Dict[str, str]` - Request-to-session mapping
- `_active_requests[request_id]["appended"]` - Pending message queue

#### 2. AgentRequestManager (`request_manager.py`)
**Purpose:** Manages active request lifecycle and cancellation

**Key Methods:**
- `cancel_request(request_id)` - Cancel active request
- `is_cancelled(request_id)` - Check cancellation status
- `register_active_request(request_id, request_entry)` - Register new request
- `unregister_active_request(request_id)` - Clean up completed request
- `get_active_requests()` - List active request IDs
- `get_request_entry(request_id)` - Get request metadata

**Manages:**
- `_active_requests: Dict[str, Dict]` - Active request registry (shared with SessionTracker)

### Component Coordination

Both components share the same `_active_requests` dictionary reference for consistency:

```python
# In Agent.__init__:
self._request_manager = AgentRequestManager(self.name)
self._session_tracker = SessionTracker(self._request_manager._active_requests)
```

This enables:
- SessionTracker to check if requests are active before appending messages
- Consistent request lifecycle tracking across both components
- No synchronization issues between components

### Usage in Agent Code

**Service Layer (agent_service.py, session_service.py):**
```python
# OLD (direct access - removed):
agent._sessions[session_id] = messages
async with agent._request_lock:
    if session_id in agent._sessions:
        del agent._sessions[session_id]

# NEW (component API):
agent._session_tracker.set_session_messages(session_id, messages)
if agent._session_tracker.has_session(session_id):
    agent._session_tracker.delete_session(session_id)
```

**Request Management:**
```python
# OLD (direct access - removed):
async with agent._request_lock:
    agent._active_requests[request_id]["cancel"].set()

# NEW (component API):
await agent._request_manager.cancel_request(request_id)
if agent._request_manager.is_cancelled(request_id):
    # Handle cancellation
```

### Benefits of Component Architecture

✅ **Encapsulation:** Internal dictionaries not exposed directly  
✅ **Testability:** Components can be tested in isolation  
✅ **Maintainability:** Clear single responsibility per component  
✅ **Extensibility:** Easy to add monitoring/metrics/hooks per component  
✅ **Type Safety:** Component methods provide better type hints than dict access

### For Plugin Developers

**You don't need to know about these internal components!** They're implementation details of the `Agent` base class. Just use the public Agent API:

- `run_events(task, session_id, ...)` - Execute agent task
- `cancel_request(request_id)` - Cancel active request  
- `get_tools()` - Define your agent's tools
- `call(tool, params)` - Handle tool execution

The component architecture is transparent to plugin developers.
