# Tool Execution System

Die Tool-Ausführung ist ein zentraler Bestandteil des AgentSystem. Sie ermöglicht es Agenten, sowohl interne Plugin-Tools als auch externe MCP-Server-Tools zu nutzen und deren Ausführung zu verwalten.

## Überblick

Das Tool-Execution-System umfasst mehrere Komponenten:

1. **Tool Discovery** - Erkennung verfügbarer Tools
2. **Tool Filtering** - Anwendung von Zugriffsbeschränkungen
3. **Tool Execution** - Parallele und sequentielle Ausführung
4. **Status Management** - Status-Updates während der Ausführung
5. **Error Handling** - Behandlung von Fehlern und Abbrüchen

## Architektur

### Hauptkomponenten

```
┌─────────────────────────────────────────────────────────────┐
│                        Agent Server                          │
│  ┌───────────────────────────────────────────────────────┐  │
│  │         MCPIntegrationManager                         │  │
│  │  - Tool Discovery                                     │  │
│  │  - Schema Building                                    │  │
│  │  - External Server Integration                        │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │         ToolExecutionManager                          │  │
│  │  - Parallel Execution                                 │  │
│  │  - Cancellation Support                               │  │
│  │  - Error Handling                                     │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
┌──────────────────┐              ┌──────────────────────┐
│  Plugin Tools    │              │  External MCP Tools  │
│  (Internal)      │              │  (Remote Servers)    │
└──────────────────┘              └──────────────────────┘
```

## Tool Discovery

### 1. Tool-Quellen

Das System sammelt Tools aus verschiedenen Quellen:

#### Plugin-Tools (Intern)
```python
# Aus dem Plugin-Registry
plugin_tools = await mcp_integration.plugin_registry.get_all_tools()
```

Beispiele:
- `backlog` - Backlog-Verwaltung
- `web_research` - Web-Recherche
- `session_manager` - Session-Verwaltung

#### Externe MCP-Tools
```python
# Aus externen MCP-Servern
external_tools = await mcp_integration.client_manager.list_all_tools()
```

Beispiele:
- `context7.resolve-library-id` - Library-ID-Auflösung
- `context7.get-library-docs` - Dokumentation abrufen
- `memory.create_entities` - Wissensgraph-Entitäten erstellen

#### Agent-eigene Tools
```python
# Tools, die vom Agenten selbst definiert werden
own_tools = agent.get_tools()
```

Beispiele:
- `basic_agent_execute_task` - Task-Ausführung (BasicAgent)
- `web_research_agent_web_research` - Web-Recherche (WebResearchAgent)
- `sysadmin_agent_execute_task` - SSH-basierte Task-Ausführung (SysAdminAgent)

### 2. Tool-Schema-Generierung

Tools werden in OpenAI-kompatible Schemas konvertiert:

```python
async def build_tool_schemas(available_tools: List[str]) -> tuple[List[Dict], Dict[str, str]]:
    """Build OpenAI-compatible tool schemas and name mappings."""
    tools_schema: List[Dict] = []
    tool_name_mapping = {}  # Maps OpenAI names to original names
    
    for tool_name in available_tools:
        if "." in tool_name:
            # External tool: context7.resolve-library-id
            schema = {
                "type": "function",
                "function": {
                    "name": "context7_resolve_library_id",  # OpenAI-kompatibel
                    "description": "[context7] Resolve library ID",
                    "parameters": {...}
                }
            }
            tool_name_mapping["context7_resolve_library_id"] = "context7.resolve-library-id"
        else:
            # Internal tool
            schema = {...}
    
    return tools_schema, tool_name_mapping
```

### 3. Tool-Filterung

Tools werden basierend auf Agent-Konfiguration gefiltert:

```python
def _filter_available_tools(self, tools: list[str], patterns: list[str]) -> list[str]:
    """Filter tools based on allow/block patterns."""
    # Beispiel patterns: ["backlog", "web_research.*", "!dangerous_tool"]
    filtered = []
    for tool in tools:
        if self._is_tool_allowed(tool, patterns):
            filtered.append(tool)
    return filtered
```

#### Beispiel-Konfiguration
```yaml
# config/agents.yaml
agents:
  research_agent:
    tools:
      - "web_research.*"      # Alle web_research Tools
      - "context7.*"          # Alle context7 Tools
      - "!context7.dangerous" # Außer dangerous
```

### 4. Custom Tool Descriptions

Agenten können Tool-Beschreibungen überschreiben:

```python
def _apply_custom_tool_descriptions(self, tools_schema: List[Dict]) -> None:
    """Apply custom tool descriptions from agent configuration."""
    if not self.agent_config.self_tool_descriptions:
        return
    
    for tool_schema in tools_schema:
        tool_name = tool_schema["function"]["name"]
        if tool_name in self.agent_config.self_tool_descriptions:
            # Override description
            tool_schema["function"]["description"] = \
                self.agent_config.self_tool_descriptions[tool_name]
```

#### Beispiel
```yaml
# config/agents.yaml
agents:
  sysadmin_agent:
    base_type: basic_agent
    self_tool_descriptions:
      sysadmin_agent_execute_task: >
        Execute administrative tasks on remote systems via SSH.
        This tool has access to production servers and should be
        used with caution.
```

## Tool Execution

### 1. Execution Flow

```python
# Produktions-Interface ist der Streaming-Generator; der fruehere
# execute_tools()-Wrapper wurde entfernt (Review G5). Der Flow darunter
# beschreibt die Logik von execute_tools_streaming().
async def execute_tools_streaming(
    tool_calls: List[Dict],           # Tool calls vom LLM
    tool_name_mapping: Dict[str, str], # OpenAI names → original names
    available_tools: List[str],        # Verfügbare Tools
    step: int,                         # Aktueller Step
    request_id: str | None = None      # Request ID für Tracking
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute all tool calls, streaming status/tool events + final results."""
    
    # 1. Parse und validiere Tool-Calls
    valid_tool_executions = []
    for tc in tool_calls:
        openai_name = tc["function"]["name"]
        tool_name = tool_name_mapping.get(openai_name, openai_name)
        
        if tool_name not in available_tools:
            # Tool nicht verfügbar → Error-Response
            continue
        
        # Parse arguments
        params = json.loads(tc["function"]["arguments"])
        valid_tool_executions.append((tc, tool_name, openai_name, params))
    
    # 2. Führe alle Tools parallel aus
    tasks = []
    for tc, tool_name, openai_name, params in valid_tool_executions:
        # Erstelle tool-spezifische request_id
        tool_request_id = f"{request_id}_{step:03d}"
        
        # Erstelle Task für parallele Ausführung
        task = self._execute_single_tool(
            tc, tool_name, openai_name, params, step, tool_request_id
        )
        tasks.append(task)
    
    # Warte auf alle Tasks (parallel)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 3. Verarbeite Ergebnisse
    tool_messages = []
    events = []
    tool_results = []
    
    for result in results:
        if isinstance(result, Exception):
            # Error handling
            tool_messages.append(create_error_message(result))
        else:
            # Normal result
            message, evt, res = result
            tool_messages.append(message)
            events.extend(evt)
            tool_results.extend(res)
    
    return tool_messages, events, tool_results
```

### 2. Single Tool Execution

```python
async def _execute_single_tool(
    tc: Dict,
    tool_name: str,
    openai_tool_name: str,
    params: Dict[str, Any],
    step: int,
    request_id: str | None = None
) -> tuple[ChatMessage, List[Dict], List[Dict]]:
    """Execute a single tool with cancellation support."""
    
    if "." in tool_name:
        # External MCP tool
        return await self._execute_external_tool(...)
    else:
        # Internal plugin tool
        return await self._execute_plugin_tool(...)
```

### 3. Plugin Tool Execution

```python
async def _execute_plugin_tool(
    tc: Dict,
    tool_name: str,
    openai_tool_name: str,
    params: Dict[str, Any],
    step: int,
    request_id: str | None = None
) -> tuple[ChatMessage, List[Dict], List[Dict]]:
    """Execute a plugin tool."""
    
    # 1. Hole Server aus Registry
    server = self._get_server_from_any_registry(tool_name)
    
    # 2. Führe Tool aus (mit Status-Support)
    result = await server.call_with_status(tool_name, params)
    
    # 3. Erstelle Response-Message
    tool_call_id = tc.get("id") or f"{tool_name}-call-{timestamp}"
    message = ChatMessage(
        role="tool",
        tool_call_id=tool_call_id,
        name=sanitize_for_llm(openai_tool_name),
        content=json.dumps(result)
    )
    
    # 4. Erstelle Events für Streaming
    events = [
        {"type": "tool_call", "tool": tool_name, "step": step},
        {"type": "tool_result", "tool": tool_name, "result": result}
    ]
    
    return message, events, [{"tool": tool_name, "result": result}]
```

### 4. External Tool Execution

```python
async def _execute_external_tool(
    tc: Dict,
    tool_name: str,
    openai_tool_name: str,
    params: Dict[str, Any],
    step: int,
    request_id: str | None = None
) -> tuple[ChatMessage, List[Dict], List[Dict]]:
    """Execute an external MCP tool."""
    
    # 1. Parse server name und tool name
    server_name, actual_tool_name = tool_name.split(".", 1)
    # z.B. "context7.resolve-library-id" → "context7", "resolve-library-id"
    
    # 2. Call external server via MCP integration
    mcp_integration = get_mcp_integration()
    result = await mcp_integration.call_tool(
        server_name,
        actual_tool_name,
        params,
        "external"
    )
    
    # 3. Erstelle Response (analog zu Plugin-Tools)
    message = ChatMessage(...)
    events = [
        {"type": "mcp_call", "server": server_name, "action": actual_tool_name},
        {"type": "mcp_result", "result": result}
    ]
    
    return message, events, [{"server": server_name, "result": result}]
```

## Parallel Execution

### Request ID Management

Jeder Tool-Call erhält eine eindeutige Request-ID für Tracking und Cancellation:

```python
# Hauptrequest: "abc123"
# Tool-Calls erhalten Suffixe:
# - "abc123_001" (erster Tool-Call)
# - "abc123_002" (zweiter Tool-Call)
# - "abc123_003" (dritter Tool-Call)

for i, (tc, tool_name, openai_name, params) in enumerate(valid_tool_executions):
    tool_request_id = await agent.next_internal_tool_request_id(original_request_id)
    # → Globaler Counter für eindeutige IDs
    
    params_with_id = params.copy()
    params_with_id["request_id"] = tool_request_id
    params_with_id["requestId"] = tool_request_id  # JS-Kompatibilität
```

### Async Gather

Tools werden parallel mit `asyncio.gather()` ausgeführt:

```python
# Erstelle Tasks
tasks = [
    self._execute_single_tool(tc1, ...),
    self._execute_single_tool(tc2, ...),
    self._execute_single_tool(tc3, ...),
]

# Führe parallel aus (return_exceptions=True für Error-Handling)
results = await asyncio.gather(*tasks, return_exceptions=True)

# Verarbeite Ergebnisse in Reihenfolge
for i, result in enumerate(results):
    if isinstance(result, Exception):
        # Handle error
    else:
        # Process result
```

## Status Management

### StatusScope Integration

Tools erhalten automatisch ein `StatusScope`-Objekt für Updates:

```python
async def call_with_status(self, action: str, params: dict[str, Any]):
    """Call tool with automatic StatusScope management."""
    from .status import get_status_bus, status_scope
    
    status_bus = get_status_bus()
    request_id = params.get("request_id") or params.get("requestId")
    
    async with status_scope(status_bus, self.name, request_id=request_id) as status:
        # Inject status object for the plugin to use
        params_with_status = params.copy()
        params_with_status["_status"] = status
        params_with_status["_request_id"] = request_id
        
        return await self.call(action, params_with_status)
```

### Status Updates in Tools

Tools können Status-Updates senden:

```python
async def my_tool(self, params: Dict[str, Any]) -> Any:
    """Example tool with status updates."""
    status = params.get("_status")
    
    if status:
        await status.progress("Starting processing...", meta={"step": 1})
    
    # Do work...
    result = await process_data()
    
    if status:
        await status.progress("Processing complete", meta={"step": 2})
    
    return result
```

### Status Events

Status-Events werden automatisch an den Client gestreamt:

```python
{
    "type": "status",
    "scope": "tool_name",
    "status": "in_progress",
    "message": "Starting processing...",
    "request_id": "abc123_001",
    "meta": {"step": 1}
}
```

## Cancellation Support

### Cancellation Flow

```python
async def _execute_with_cancellation(
    self,
    tc: Dict,
    tool_name: str,
    openai_tool_name: str,
    params: Dict[str, Any],
    step: int,
    request_id: str
) -> tuple[ChatMessage, List[Dict], List[Dict]]:
    """Execute tool with cancellation support."""
    
    cancellation_manager = get_cancellation_manager()
    
    # 1. Check main request cancellation
    main_token = cancellation_manager.get_token(request_id)
    if main_token and main_token.is_cancelled:
        return self._create_cancelled_response(tc, tool_name, ...)
    
    # 2. Create tool-specific cancellation context
    tool_request_id = f"{request_id}_{step:03d}"
    async with cancellable_operation(tool_request_id, cleanup_timeout=30.0) as tool_token:
        
        # 3. Add cancellation token to params
        params_with_token = params.copy()
        params_with_token["_cancellation_token"] = tool_token
        
        # 4. Execute tool
        task = asyncio.create_task(
            self._execute_plugin_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id)
        )
        
        # 5. Register task for forced cancellation
        cancellation_manager.register_task(tool_request_id, task)
        
        # 6. Wait for completion or cancellation
        try:
            return await task
        except asyncio.CancelledError:
            # Forced cancellation
            return self._create_cancelled_response(tc, tool_name, ..., forced=True)
        except CancellationError as e:
            # Graceful cancellation
            return self._create_cancelled_response(tc, tool_name, ..., forced=e.forced)
```

### Cancellation in Tools

Tools können Cancellation-Status prüfen:

```python
async def long_running_tool(self, params: Dict[str, Any]) -> Any:
    """Tool that supports graceful cancellation."""
    token = params.get("_cancellation_token")
    
    for i in range(100):
        # Check if cancelled
        if token and token.is_cancelled:
            logger.info("Tool cancelled, cleaning up...")
            # Perform cleanup
            raise CancellationError("Tool was cancelled")
        
        # Do work
        await process_item(i)
    
    return {"processed": 100}
```

## Error Handling

### Exception Types

1. **Tool nicht verfügbar**
```python
if tool_name not in available_tools:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=openai_tool_name,
        content=json.dumps({"error": f"Tool '{tool_name}' is not available."})
    )
```

2. **Tool-Ausführungsfehler**
```python
try:
    result = await server.call(tool_name, params)
except Exception as e:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=openai_tool_name,
        content=json.dumps({"error": f"Tool invocation failed: {str(e)}"})
    )
```

3. **Cancellation**
```python
if token.is_cancelled:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=openai_tool_name,
        content=json.dumps({
            "error": "Tool was cancelled.",
            "cancelled": True,
            "forced": token.is_forced
        })
    )
```

4. **GeneratorExit** (Async Generator Tools)
```python
except GeneratorExit:
    # Tool wurde vorzeitig geschlossen
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=openai_tool_name,
        content=json.dumps({"error": "Tool execution was cancelled (GeneratorExit)"})
    )
```

### Error Events

Fehler werden als Events gestreamt:

```python
{
    "type": "error",
    "message": "Tool 'unknown_tool' is not available.",
    "request_id": "abc123"
}

{
    "type": "tool_force_cancelled",
    "tool": "long_running_tool",
    "request_id": "abc123_001"
}
```

## Content Sanitization

### LLM-sichere Ausgabe

Tool-Ergebnisse werden vor der Weitergabe an das LLM sanitiert:

```python
from agent_system.llm.text_sanitizer import sanitize_for_llm, sanitize_json_content

# Tool name sanitization
message = ChatMessage(
    role="tool",
    tool_call_id=tc.get("id"),
    name=sanitize_for_llm(openai_tool_name),  # Entfernt problematische Zeichen
    content=sanitize_json_content(json.dumps(result))  # Escaped JSON
)
```

### Serialization für Events

Events enthalten nur JSON-serialisierbare Daten:

```python
def _make_params_serializable(self, params: Dict[str, Any]) -> Dict[str, Any]:
    """Create JSON-serializable copy of params."""
    serializable_params = {}
    for key, value in params.items():
        # Skip internal objects (starting with "_")
        if key.startswith('_'):
            continue
        
        # Test if JSON-serializable
        try:
            json.dumps(value)
            serializable_params[key] = value
        except (TypeError, ValueError):
            # Skip non-serializable values
            continue
    
    return serializable_params
```

## Beispiele

### Beispiel 1: Einfache Tool-Ausführung

```python
# LLM ruft Tool auf
tool_calls = [
    {
        "id": "call_abc123",
        "function": {
            "name": "backlog_get_status",
            "arguments": "{}"
        }
    }
]

# Ergebnis einsammeln (Tests: shared Helper; Produktion konsumiert den Stream)
from tool_execution_test_helpers import execute_tools_collect
messages, events, results = await execute_tools_collect(tool_execution_manager,
    tool_calls=tool_calls,
    tool_name_mapping={"backlog_get_status": "backlog"},
    available_tools=["backlog"],
    step=0,
    request_id="req_123"
)

# Result:
# messages = [ChatMessage(role="tool", content='{"total": 5, "open": 3}')]
# events = [
#     {"type": "tool_call", "tool": "backlog", ...},
#     {"type": "tool_result", "tool": "backlog", ...}
# ]
```

### Beispiel 2: Parallele Tool-Ausführung

```python
# LLM ruft mehrere Tools gleichzeitig auf
tool_calls = [
    {"id": "call_1", "function": {"name": "backlog_get_status", "arguments": "{}"}},
    {"id": "call_2", "function": {"name": "web_research_search", "arguments": '{"query": "Python"}'}},
    {"id": "call_3", "function": {"name": "context7_resolve_library_id", "arguments": '{"libraryName": "fastapi"}'}}
]

# Alle Tools werden parallel ausgeführt
messages, events, results = await execute_tools_collect(tool_execution_manager,
    tool_calls=tool_calls,
    tool_name_mapping={...},
    available_tools=["backlog", "web_research", "context7.resolve-library-id"],
    step=0,
    request_id="req_456"
)

# Jeder Tool-Call erhält eine eindeutige ID:
# - backlog: req_456_001
# - web_research: req_456_002
# - context7: req_456_003
```

### Beispiel 3: External MCP Tool

```python
# LLM ruft externes Tool auf
tool_calls = [
    {
        "id": "call_ext",
        "function": {
            "name": "context7_get_library_docs",  # OpenAI-kompatibel
            "arguments": '{"context7CompatibleLibraryID": "/fastapi/fastapi"}'
        }
    }
]

# Tool name mapping
tool_name_mapping = {
    "context7_get_library_docs": "context7.get-library-docs"  # Original name
}

# Ausführung über MCP integration
messages, events, results = await execute_tools_collect(tool_execution_manager, ...)

# Intern wird aufgerufen:
# mcp_integration.call_tool(
#     server_name="context7",
#     tool_name="get-library-docs",
#     params={"context7CompatibleLibraryID": "/fastapi/fastapi"},
#     category="external"
# )
```

### Beispiel 4: Tool mit Status-Updates

```python
# Plugin-Tool mit Status-Support
class MyPlugin(PluginServer):
    async def long_task(self, params: Dict[str, Any]) -> Any:
        status = params.get("_status")
        
        # Start
        if status:
            await status.progress("Starting...", meta={"progress": 0})
        
        # Processing
        for i in range(10):
            await asyncio.sleep(1)
            if status:
                await status.progress(
                    f"Processing step {i+1}/10",
                    meta={"progress": (i+1) * 10}
                )
        
        # Complete
        if status:
            await status.complete("Done!", meta={"progress": 100})
        
        return {"result": "success"}

# Client erhält Status-Events:
# {"type": "status", "message": "Starting...", "meta": {"progress": 0}}
# {"type": "status", "message": "Processing step 1/10", "meta": {"progress": 10}}
# ...
# {"type": "status", "message": "Done!", "meta": {"progress": 100}}
```

### Beispiel 5: Tool-Cancellation

```python
# Client startet Request
response = await agent.run_events(
    task="Long running task",
    request_id="req_789"
)

# Client cancelt Request
await cancellation_manager.cancel_request("req_789")

# Tool prüft Cancellation
async def my_tool(self, params: Dict[str, Any]) -> Any:
    token = params.get("_cancellation_token")
    
    for i in range(1000):
        if token and token.is_cancelled:
            # Cleanup
            await cleanup()
            raise CancellationError("Cancelled by user")
        
        await process_item(i)

# Client erhält Cancelled-Response:
# ChatMessage(
#     role="tool",
#     content='{"error": "Tool was cancelled.", "cancelled": true}'
# )
```

## Best Practices

### 1. Tool-Design

- **Idempotenz**: Tools sollten idempotent sein (mehrfache Aufrufe mit gleichen Parametern = gleiches Ergebnis)
- **Error Handling**: Immer sinnvolle Fehlermeldungen zurückgeben
- **Status Updates**: Bei langen Operationen regelmäßig Status-Updates senden
- **Cancellation**: Cancellation-Token prüfen bei langläufigen Operationen

### 2. Parameter-Design

- **JSON-serialisierbar**: Alle Parameter müssen JSON-serialisierbar sein
- **Klare Namen**: Verwende beschreibende Parameter-Namen
- **Validierung**: Validiere Parameter früh
- **Defaults**: Biete sinnvolle Default-Werte an

### 3. Performance

- **Parallel Execution**: Nutze parallele Ausführung wo möglich
- **Timeouts**: Setze Timeouts für externe Calls
- **Caching**: Cache teure Operationen
- **Streaming**: Streame große Datenmengen statt sie komplett zu laden

### 4. Monitoring

- **Request IDs**: Verwende Request-IDs für Tracking
- **Logging**: Logge wichtige Ereignisse (Start, Ende, Fehler)
- **Metrics**: Tracke Ausführungszeiten und Fehlerraten
- **Events**: Emittiere Events für wichtige Zustandsänderungen

## Troubleshooting

### Problem: Tool wird nicht gefunden

**Symptom**: `"Unknown tool: <tool_name>"`

**Ursachen**:
1. Tool ist nicht in `available_tools` Liste
2. Tool wurde durch Filter blockiert
3. Tool-Name-Mapping ist falsch

**Lösung**:
```python
# Debug verfügbare Tools
logger.debug(f"Available tools: {available_tools}")
logger.debug(f"Tool name mapping: {tool_name_mapping}")

# Prüfe Filter-Konfiguration
agent.agent_config.tools  # Allowed/blocked patterns
```

### Problem: Tool-Ausführung hängt

**Symptom**: Tool reagiert nicht mehr

**Ursachen**:
1. Tool wartet auf Input/Lock
2. Deadlock in async Code
3. Fehlende Timeouts

**Lösung**:
```python
# Implementiere Timeouts
try:
    result = await asyncio.wait_for(
        server.call(tool_name, params),
        timeout=30.0
    )
except asyncio.TimeoutError:
    raise RuntimeError("Tool execution timeout")

# Prüfe Cancellation regelmäßig
for i in range(1000):
    if token and token.is_cancelled:
        raise CancellationError()
    await process_item(i)
```

### Problem: Status-Updates kommen nicht an

**Symptom**: Keine Status-Events im Client

**Ursachen**:
1. `_status` nicht in Params injiziert
2. `call_with_status()` nicht verwendet
3. Status-Bus nicht konfiguriert

**Lösung**:
```python
# Verwende immer call_with_status
result = await server.call_with_status(tool_name, params)

# Prüfe Status-Objekt in Tool
status = params.get("_status")
if status:
    await status.progress("Update message")
else:
    logger.warning("No status object available")
```

### Problem: Request ID Kollisionen

**Symptom**: Events werden falschen Requests zugeordnet

**Ursachen**:
1. Request IDs nicht eindeutig
2. Tool-spezifische Suffixe fehlen

**Lösung**:
```python
# Verwende Agent-Counter für eindeutige IDs
tool_request_id = await agent.next_internal_tool_request_id(request_id)

# Oder manuelle Suffixe
tool_request_id = f"{request_id}_{step:03d}_{i:03d}"
```

## Siehe auch

- [Plugin Architecture](plugin_architecture.md) - Plugin-System
- [MCP Configuration](mcp_configuration.md) - MCP-Server-Konfiguration
- [Status Design](status_design.md) - Status-System
- [Context Management](context_management.md) - Cancellation-System
