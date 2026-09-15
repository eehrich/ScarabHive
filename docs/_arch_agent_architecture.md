# Agent Architecture

## Overview

The Agent System provides two base classes for building intelligent agents with MCP (Model Context Protocol) integration:

1. **`Agent`** - Core agent logic for LLM interaction, tool orchestration, and conversation management
2. **`SchemaBasedAgent`** - Extends Agent with automatic `schema.yaml` loading for declarative tool definitions

This architecture follows the **Single Responsibility Principle** and maintains consistency with the existing `SchemaBasedMCPServer` pattern used for simpler MCP tools.

## Architecture Diagram

```
MCPServer (base protocol implementation)
│
├─ SchemaBasedMCPServer (simple schema-based tools)
│  ├─ DateTimeServer
│  ├─ WebScraperServer
│  ├─ LLMRouterServer
│  └─ ... (other schema-based tools)
│
└─ Agent (core agent logic)
   │
   └─ SchemaBasedAgent (adds schema.yaml loading)
      ├─ BasicAgent
      ├─ WebResearchAgent
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
                        "param": {
                            "type": "string",
                            "description": "Dynamic parameter"
                        }
                    },
                    "required": ["param"]
                }
            }
        ]
    
    async def handle_custom_tool(self, arguments: dict) -> str:
        """Handle custom tool execution."""
        return f"Processed: {arguments['param']}"
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
    
    async def handle_my_tool(self, arguments: dict) -> str:
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

### SchemaBasedMCPServer vs SchemaBasedAgent

Both follow the same pattern but serve different purposes:

| Feature | SchemaBasedMCPServer | SchemaBasedAgent |
|---------|---------------------|------------------|
| **Purpose** | Simple MCP tools | Intelligent agents with LLM |
| **Base Class** | `MCPServer` | `Agent` (extends `MCPServer`) |
| **Schema Loading** | `schema.yaml` | `schema.yaml` |
| **Tool Execution** | Direct handlers | Via Agent orchestration |
| **LLM Integration** | No | Yes (core feature) |
| **Use Case** | Utility tools (datetime, scraper) | Agentic workflows (research, coding) |

**Example Comparison:**

```python
# SchemaBasedMCPServer - Simple tool
class DateTimeServer(SchemaBasedMCPServer):
    async def handle_get_current_time(self, arguments: dict) -> str:
        return datetime.now().isoformat()

# SchemaBasedAgent - Intelligent agent
class ResearchAgent(SchemaBasedAgent):
    async def handle_research(self, arguments: dict) -> str:
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

### Schema Loading (SchemaBasedMixin)

Both `SchemaBasedMCPServer` and `SchemaBasedAgent` inherit from `SchemaBasedMixin`, which provides common schema loading functionality:

**Location:** `src/agent_system/mcp/schema_mixin.py`

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
class SchemaBasedMixin:
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
class SchemaBasedMCPServer(MCPServer, SchemaBasedMixin):
    pass  # Direct tool name → method mapping

class SchemaBasedAgent(Agent, SchemaBasedMixin):
    def _get_method_name(tool_name):
        # Strip agent name prefix: "basic_agent_execute_task" → "execute_task"
        return tool_name.removeprefix(f"{self.name}_")
```

### Automatic Method Routing

The `SchemaBasedMixin.call()` dispatcher automatically routes tool calls to methods:

**For SchemaBasedMCPServer:**
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

# Plugin implements method (no "handle_" prefix needed with SchemaBasedMixin)
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
# Just implement the method - SchemaBasedMixin.call() handles routing
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

**`plugin.yaml`:**
```yaml
name: my_agent
version: "1.0.0"
description: "My custom agent"
plugin_type: basic_agent  # or web_research_agent
author: "Your Name"
server_config:
  # Agent-specific configuration
  model: "gpt-4"
  max_tokens: 2000
```

**Plugin configuration in `plugins:` section:**
```yaml
plugins:
  servers:
    research_agent_1:
      type: my_agent
      enabled: true
      config:
        model: "gpt-4"
        max_tokens: 2000
```

## Testing

### Testing Agents

Both agent types should be tested similarly:

```python
import pytest
from agent_system.config.models import AgentSystemConfig, MCPServerConfig

@pytest.fixture
def agent(mock_system_config: AgentSystemConfig):
    """Create agent instance for testing."""
    mcp_config = MCPServerConfig(
        plugin_name="my_agent",
        instance_name="test_agent",
        enabled=True,
        config={}
    )
    agent = MyAgent(
        system_config=mock_system_config,
        mcp_config=mcp_config,
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
    assert hasattr(agent, "handle_my_tool")
    
    # 3. Test execution
    result = await agent.handle_my_tool({"input": "test"})
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
   └── plugin.yaml
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
1. Verify tool handler method exists: `handle_{tool_name}()`
2. Check method signature: `async def handle_tool(self, arguments: dict) -> str`
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

## LLM-Fallback und Sperren

Fällt ein LLM aus, läuft der Agent auf dem nächsten Profil seiner Kette weiter.
Ob ein LLM **gesperrt** ist, gehört dabei dem LLM, nicht dem Agenten: die
Sperre gilt für jeden Agenten im Prozess (`src/agent_system/llm/model_health.py`).

### Konfiguration

Fallbacks stehen als **Kette** direkt in `llm_profile` (seit 2026-07:
Liste = `[primär, fallback1, fallback2, ...]`; der entfernte Schlüssel
`llm_profile_fallbacks` bricht das Laden der Config ab):

```yaml
my_agent:
  type: basic_agent
  agent_config:
    llm_profile: ["gemini", "openai", "anthropic"]   # primär + Fallback-Kette
    llm_profile_advanced: ["gpt-large", "claude"]    # optional: Advanced-Kette (use_advanced_model / Auto-Eskalation)
    fallback_recovery_seconds: 1800                  # längste Sperre, die dieser Agent setzt (Default: 3600)
```

Volle Ketten-Semantik (Advanced-Kette, `llm_params`, Migrationsskript
`scripts/migrate_llm_profiles.py`): `docs/basic_agent_llm_profiles.md`.

### Was eine Sperre auslöst — und was nicht

| Fehler | Sperre des LLM (für alle Agenten) | Dieser Request |
|---|---|---|
| `LLMRateLimitError` (429) | 60 s, verdoppelt bei jedem weiteren Fehlschlag bis `fallback_recovery_seconds`; `retry_after` des Anbieters ist die Untergrenze | nächstes Profil der Kette |
| `LLMQuotaExhaustedError` | sofort `fallback_recovery_seconds` | nächstes Profil der Kette |
| HTTP 401/402/403/404 | sofort `fallback_recovery_seconds` | nächstes Profil; war die Basis gescheitert, wird es Basis des Laufs |
| 5xx, Verbindungsfehler, jeder andere 4xx (400/408/409/413/422 …) | keine | nächstes Profil; war die Basis gescheitert, wird es Basis des Laufs |
| Fehler im Antwort-Body (HTTP 200 mit `error`) | keine | nächstes Profil; war die Basis gescheitert, wird es Basis des Laufs; ist die Kette aufgebraucht, endet der Lauf mit einem `error`-Event |
| lokal keine Dateideskriptoren mehr (EMFILE) | keine | kein Wechsel, Fehler |

**Ein Burst ist ein Fehlschlag.** Ein 429 auf einen Aufruf, der losging, bevor
die Sperre gesetzt wurde, verdoppelt sie nicht: sieben Requests, die gerade
unterwegs sind, wenn das Minutenfenster zugeht, sperren 60 s, nicht eine
Stunde. Kommt so ein Nachzügler erst nach Ablauf der Sperre an (die Clients
wiederholen ein 429 selbst, bevor sie es melden), sperrt er gar nichts mehr —
sonst machte er eine Probe zunichte, die das LLM gerade gesund findet. Ein
`fallback_recovery_seconds` von 0 sperrt nichts. **Keine Sperre verkürzt eine längere, die noch läuft** — ein Agent mit
kurzem `fallback_recovery_seconds` kürzt die Kontingent-Sperre eines anderen
nicht auf seine.

Der Schlüssel eines LLM ist **(Endpunkt, Key, Modell)** des Clients:
- Endpunkt ist die `base_url`; ein Client ohne (SDK-Clients, Batch-Client) ist
  sein eigener Endpunkt, benannt nach seiner Klasse — ein Batch-Kontingent ist
  nicht das Sync-Kontingent desselben Modells.
- Key ist ein Fingerabdruck des API-Keys (nie der Key selbst): ein abgelehnter
  oder erschöpfter Key sagt nichts über einen anderen.
- Zwei Profile mit demselben Modell an derselben URL mit demselben Key sind
  ein LLM, egal welche Client-Klasse sie spricht. Clients ohne eigene URL
  (SDK, Batch) sind ein Endpunkt je Klasse, egal welchen Server sie erreichen.

Das Provider-Routing gehört nicht dazu — ein 429 von einem OpenRouter-Backend
sperrt das Modell auch für ein Profil mit anderem Routing.

### Wie ein Schritt sein LLM wählt

Vor jedem Schritt, **vor** den Pre-LLM-Hooks:

1. Ist das Eskalationsfenster offen und das Advanced-LLM frei, läuft der
   Schritt darauf. Ist es gesperrt, gilt dieser Schritt als nicht eskaliert
   (das Fenster bleibt offen) und es geht mit 2. weiter.
2. Gewünscht ist dann das Override des Requests (`--llm`, Auswahl im Chat),
   sonst die Basis des Laufs. Ist dessen LLM frei, läuft der Schritt darauf.
3. Sonst läuft er auf dem ersten freien Profil der Kette.
4. Ist keines frei, läuft er trotzdem auf dem gewünschten — ein gesperrtes LLM
   ist besser als keines.

Scheitert der Aufruf, versucht der Schritt die übrigen Profile der Kette —
freie zuerst, dann gesperrte, jedes einmal. Die Basis des Laufs gehört dazu,
wenn der Schritt nicht auf ihr lief: nach einer gescheiterten Eskalation als
**erstes** Glied (das Advanced-Modell sagt nichts über die Basis), nach einem
gescheiterten Weg um eine gesperrte Basis herum als **letztes**. So bricht der
Lauf nicht ab, solange noch ein LLM übrig ist.

Eine ausdrückliche Wahl ist **keine** Ausnahme: ein gesperrtes LLM bleibt
gesperrt, auch wenn es im Chat gewählt wird. Ein anderes, freies LLM läuft
dagegen sofort — die Sperre des einen hält es nicht auf.

Dauern die Hooks, während eine Sperre gesetzt oder aufgehoben wird, wählt der
Schritt danach neu (`model_health.version`). Jeder Modellwechsel, auch der
zurück, entfernt die Reasoning-Artefakte des vorigen Modells aus dem Verlauf.

### Aufheben

- **Eine Antwort hebt die Sperre für alle auf** — sofern der Aufruf nach dem
  Setzen der Sperre losging. Eine Antwort auf einen älteren Aufruf sagt nichts
  über das LLM danach.
- **Nach Ablauf** probiert genau ein Request das LLM; die anderen behandeln es
  weiter als gesperrt, bis die Probe antwortet (Aufheben) oder mit einem
  sperrenden Fehler scheitert (neue Sperre: bei 429 doppelt so lang, bei
  Kontingent oder abgelehntem Key wieder `fallback_recovery_seconds`). Endet
  die Probe ohne Urteil (5xx, Abbruch), ist das LLM nach 120 s für die nächste
  frei; erneutes Fragen desselben Requests verlängert diese Frist nicht (nach
ihrem Ablauf wird die Probe neu vergeben, an wen zuerst fragt). Wählt der Request nach den
  Hooks doch ein anderes LLM, gibt er die Probe sofort zurück. So laufen nicht
  zwanzig Agenten gleichzeitig in dasselbe 429.

### Grenzen

- **Pro Prozess.** Die Writer-Job-Worker sind eigene Prozesse mit eigenen
  Sperren; was die API sperrt, erreicht sie nicht.
- **Alle Glieder versucht und der Aufruf scheitert:** der Fehler des letzten
  Versuchs geht an den Aufrufer (beim Fehler im Antwort-Body: ein `error`-Event).

### Statusmeldungen

```json
{
  "type": "progress",
  "message": "Rate limit hit, switching to openai, retry in 60s",
  "meta": {"step": 3, "fallback": "openai", "blocked_seconds": 60.0}
}
```

Ein Schritt, der um ein gesperrtes LLM herumläuft, meldet sich als
`Calling LLM (openai:fallback)`. Im Log:

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
│   ├── mcp_integration.py (MCP protocol handling, external tool schemas)
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

> Architektur-Befunde und offene Verbesserungen: see
> `docs/agent_package_architecture_review.md`.

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
- `_appended_messages: Dict[str, List[str]]` - Pending message queue

#### 2. AgentRequestManager (`request_manager.py`)
**Purpose:** Manages active request lifecycle and cancellation

**Key Methods:**
- `cancel_request(request_id, status_bus)` - Cancel active request
- `is_cancelled(request_id)` - Check cancellation status
- `register_active_request(request_id, session_id)` - Register new request
- `unregister_active_request(request_id)` - Clean up completed request
- `get_active_requests()` - List active request IDs
- `get_request_entry(request_id)` - Get request metadata

**Manages:**
- `_active_requests: Dict[str, Dict]` - Active request registry (shared with SessionTracker)

### Component Coordination

Both components share the same `_active_requests` dictionary reference for consistency:

```python
# In Agent.__init__:
self._request_manager = AgentRequestManager(name=self.name)
self._session_tracker = SessionTracker(
    shared_active_requests=self._request_manager._active_requests
)
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
await agent._request_manager.cancel_request(request_id, status_bus)
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
