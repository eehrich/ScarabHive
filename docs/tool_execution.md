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
- `todo` - Aufgabenliste
- `tavily_search` - Web-Suche
- `datetime` - Datum und Uhrzeit

#### Externe MCP-Tools
```python
# Aus externen MCP-Servern (Schlüssel "external_servers")
all_tools = await mcp_integration.list_all_tools()
external_tools = all_tools["external_servers"]
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

Tools werden über `agent_config.tools.allowed` und `agent_config.tools.blocked` gefiltert, zweistufig (`servers/agent/tool_schema_builder.py`): `server_matches_patterns` wählt bei der Discovery die Server bzw. externen Punkt-Tools aus, nach der Expansion in Einzel-Tools entscheidet der Tool-Filter; `blocked` wird danach auf die Einzel-Tools angewendet. Muster: `*`, `server/*`, `server/tool`, exakter Name, `ext.*` für externe Server, sonst fnmatch-Globs. Eine leere `allowed`-Liste bedeutet: nichts erlaubt.

#### Beispiel-Konfiguration
```yaml
# config/agents/<name>.yaml
plugins:
  servers:
    research_agent:
      agent_config:
        tools:
          allowed:
            - "tavily_search/*"  # Alle tavily_search-Tools
            - "context7.*"     # Alle Tools des externen Servers context7
          blocked:
            - "context7.dangerous"
```

### 4. Custom Tool Descriptions

Server-Instanzen können die Beschreibungen ihrer eigenen Tools überschreiben. `self_tool_descriptions` steht auf dem Server-Eintrag (nicht in `agent_config`) und wird in `MCPServer._apply_custom_tool_descriptions` (`mcp/base.py`) angewendet; Einträge für nicht existierende Tools werden mit Warnung übersprungen.

```python
def _apply_custom_tool_descriptions(self, tools_schema: List[dict]) -> None:
    """Apply custom self tool descriptions from MCP configuration."""
    self_tool_descriptions = getattr(self.mcp_config, 'self_tool_descriptions', None)
    if not self_tool_descriptions:
        return
    for tool_name, new_desc in self_tool_descriptions.items():
        # unknown tool_name -> logger.warning, skip
        ...  # tool_schema["function"]["description"] = new_desc
```

#### Beispiel
```yaml
# config/agents/sysadmin_agent.yaml
plugins:
  servers:
    sysadmin_agent:
      type: multi_turn_agent
      self_tool_descriptions:
        sysadmin_agent_execute_task: >
          Execute administrative tasks on remote systems via SSH.
          This tool has access to production servers and should be
          used with caution.
```

## Tool Execution

### 1. Execution Flow

```python
# Produktions-Interface ist der Streaming-Generator (verkürzt).
async def execute_tools_streaming(
    tool_calls: List[Dict],           # Tool calls vom LLM
    tool_name_mapping: Dict[str, str], # OpenAI names → original names
    available_tools: List[str],        # Verfügbare Tools
    step: int,                         # Aktueller Step
    request_id: str | None = None,     # Request ID für Tracking
    session_id: str | None = None,     # wird als _session_id injiziert
    user_id: str | None = None,        # wird als _user_id injiziert
    status_forwarder: Optional[StatusEventForwarder] = None  # pro Request
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute all tool calls, streaming status/tool events + final results."""
    
    # 1. Parse und validiere Tool-Calls
    valid_tool_executions = []
    for tc in tool_calls:
        openai_name = tc["function"]["name"]
        tool_name = tool_name_mapping.get(openai_name, openai_name)
        
        # Argumente parsen (utils.json_utils.parse_tool_arguments). Kaputtes
        # JSON wird nicht repariert: Error-Message "JSONParseError", Call
        # wird NICHT ausgeführt. Keine Validierung gegen das JSON-Schema.
        params, parse_problem = parse_tool_arguments(tc["function"]["arguments"])
        
        # Vom Modell gelieferte Runtime-Parameter ("_"-Keys, request_id/requestId) werden verworfen
        params = {k: v for k, v in params.items()
                  if not k.startswith("_") and k not in ("request_id", "requestId")}
        
        if tool_name not in available_tools:
            # Error-Message "ToolNotFoundError"
            continue
        
        valid_tool_executions.append((tc, tool_name, openai_name, params))
    
    # 2. Starte alle Tools als Tasks
    tasks = []
    for i, (tc, tool_name, openai_name, params) in enumerate(valid_tool_executions):
        # Tool-spezifische request_id über den Agent-Counter
        tool_request_id = await self._agent.next_internal_tool_request_id(request_id)
        params["request_id"] = params["requestId"] = tool_request_id
        tasks.append(asyncio.create_task(self._execute_single_tool(
            tc, tool_name, openai_name, params, step, tool_request_id,
            session_id, user_id, main_request_id=request_id)))
    
    # 3. Pollen (asyncio.wait, 50 ms) und dabei Status-Events durchreichen
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(pending, timeout=0.05,
                                           return_when=asyncio.FIRST_COMPLETED)
        for status_event in status_forwarder.get_pending_events():
            yield {"type": "status", "event": status_event}
        # fertige Tasks einsammeln; Exception/CancelledError → Error-Message
    
    # 4. Messages in Aufruf-Reihenfolge sortieren (Gemini verlangt das)
    yield {"type": "tool_events", "events": events}
    yield {"type": "complete", "messages": tool_messages, "results": results}
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
    
    if request_id:
        # Mit Request-ID immer über den Cancellation-Pfad (siehe unten)
        return await self._execute_with_cancellation(...)
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
    
    # 1. Hole Server aus Registry (Fallback: eigenes Tool des Agenten)
    server = self._agent._get_server_from_any_registry(tool_name)
    
    # 2. Runtime-Parameter injizieren (inject_runtime_params):
    #    _session_id, _user_id, _request_id, _agent_name, _agent
    params = inject_runtime_params(params, session_id=session_id,
                                   user_id=user_id, request_id=request_id,
                                   agent=self._agent)
    
    # 3. Führe Tool aus (call_with_status injiziert zusätzlich _status)
    result = await server.call_with_status(openai_tool_name, params)
    
    # 4. _multimodal_content herausziehen (siehe multimodal_tool_responses_design.md)
    multimodal_content = result.pop("_multimodal_content", None) if isinstance(result, dict) else None
    
    # 5. Erstelle Response-Message
    tool_call_id = tc.get("id") or f"{tool_name}-call-{timestamp}"
    message = ChatMessage(
        role="tool",
        tool_call_id=tool_call_id,
        name=openai_tool_name,
        content=sanitize_json_content(json.dumps(result, ensure_ascii=False, default=str)),
        multimodal_content=multimodal_content
    )
    
    # 6. Erstelle Events für Streaming
    events = [
        {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": openai_tool_name, ...},
        {"type": "mcp_result", "step": step + 1, "server": tool_name, "result": result, ...}
    ]
    
    return message, events, [{"server": tool_name, "action": openai_tool_name, "result": result, ...}]
```

Ein Plugin-Tool sieht damit neben den Modell-Argumenten (jeweils soweit der Wert gesetzt ist): `request_id`/`requestId`, `_request_id`, `_session_id`, `_user_id`, `_agent_name`, `_agent`, `_status` und (bei vorhandener Request-ID) `_cancellation_token`. Vom Modell gelieferte `_`-Keys sowie `request_id`/`requestId` werden vorher verworfen: die Request-ID gehört dem Framework, sonst liefen Abbruch und Status-Routing auf dem Modell-Wert.

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
    
    # 2. Call external server via MCP integration des Agenten.
    #    Nur JSON-serialisierbare Parameter ohne "_"-Keys verlassen den
    #    Prozess -- keine Runtime-Parameter, kein Status-Objekt, kein Token.
    serializable_params = self._make_params_serializable(params)
    mcp_integration = self._agent._mcp_integration_manager.mcp_integration
    result = await mcp_integration.call_tool(
        server_name,
        actual_tool_name,
        serializable_params,
        "external"
    )
    
    # 3. Erstelle Response (analog zu Plugin-Tools, ohne multimodal_content)
    message = ChatMessage(...)
    events = [
        {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": actual_tool_name, ...},
        {"type": "mcp_result", "step": step + 1, "server": tool_name, "result": result, ...}
    ]
    
    return message, events, [{"server": tool_name, "action": actual_tool_name, "result": result, ...}]
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
    # → Counter pro Agent-Instanz (Lock-geschützt), zählt über Steps weiter;
    #   ohne Agent: f"{original_request_id}_{i+1:03d}"
    
    params_with_id = params.copy()
    params_with_id["request_id"] = tool_request_id
    params_with_id["requestId"] = tool_request_id  # JS-Kompatibilität
```

### Parallele Tasks

Tools laufen als `asyncio.create_task()` parallel; die Schleife wartet mit `asyncio.wait(..., timeout=0.05, return_when=FIRST_COMPLETED)`, damit Status-Events (z.B. von Sub-Agents) schon während der Ausführung gestreamt werden. Es gibt kein Iterationslimit.

```python
pending = set(tasks)
while pending:
    done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
    # Status-Events weiterreichen ...
    for task in done:
        try:
            tool_message, events, tool_results = task.result()
        except asyncio.CancelledError:
            # {"error": "Tool 'x' was cancelled."} + Event tool_cancelled
        except Exception as e:
            # {"error": "Tool 'x' execution failed: ...", "type": ...} + Event tool_error

# Danach nach ursprünglichem Index sortieren -- Gemini verlangt
# function_response in der Reihenfolge der function_calls.
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
    
    # Scope-Name: "<server>.<methode>()", z.B. "writer_path_validate" -> "writer.path_validate()"
    method_name = action.replace(f"{self.name}_", "") if action.startswith(f"{self.name}_") else action
    scope_name = f"{self.name}.{method_name}()"
    
    async with status_scope(status_bus, scope_name, request_id=request_id) as status:
        # Inject status object for the plugin to use
        params_with_status = params.copy()
        params_with_status["_status"] = status
        if request_id:
            params_with_status["_request_id"] = request_id
        
        result = await self.call(action, params_with_status)
        # Sicherheitsnetz: gibt der Handler ein Fehler-Ergebnis zurück, ohne
        # selbst status.error/end zu melden, sendet call_with_status
        # status.error(...) statt END "completed".
        return result
```

Ohne eigenen Aufruf von `end()`/`error()` sendet der Scope beim Verlassen automatisch `END` („completed") bzw. bei einer Exception `ERROR` („failed: …").

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

`StatusScope` kennt nur `progress()`, `end()` und `error()`. Status-Events werden über den `StatusEventForwarder` an den Client gestreamt (Filter: gleiche Request-ID oder Präfix `<request_id>_`):

```python
{
    "type": "status",
    "server": "my_plugin.my_tool()",
    "request_id": "abc123_001",
    "message": "Starting processing...",
    "phase": "progress",        # start | progress | end | error
    "level": "info",
    "timestamp": "2026-09-15T10:00:00",
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
    request_id: str,                  # tool-spezifische ID ("abc123_001")
    session_id: str | None = None,
    user_id: str | None = None,
    main_request_id: str | None = None  # Root-ID ("abc123")
) -> tuple[ChatMessage, List[Dict], List[Dict]]:
    """Execute tool with cancellation support."""
    
    cancellation_manager = get_cancellation_manager()
    
    # 1. Check main request cancellation -- das Token liegt unter der Root-ID
    main_token = cancellation_manager.get_token(main_request_id or request_id)
    if main_token and main_token.is_cancelled:
        return self._create_cancelled_response(tc, tool_name, ..., forced=main_token.is_forced)
    
    # 2. Create tool-specific cancellation context
    #    cleanup_timeout aus agent_config.timeouts.tool_cleanup_timeout (Default 30s)
    tool_request_id = f"{request_id}_{step:03d}"
    async with cancellable_operation(tool_request_id, cleanup_timeout=cleanup_timeout) as tool_token:
        
        # 3. Add cancellation token to params
        params_with_token = params.copy()
        params_with_token["_cancellation_token"] = tool_token
        
        # 4. Execute tool (_execute_plugin_tool oder _execute_external_tool)
        task = asyncio.create_task(
            self._execute_plugin_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id, session_id, user_id)
        )
        
        # 5. Register task for forced cancellation
        cancellation_manager.register_task(tool_request_id, task)
        
        # 6. Wait for completion or cancellation
        try:
            return await task
        except asyncio.CancelledError:
            # Forced cancellation
            return self._create_cancelled_response(tc, tool_name, ..., forced=True)
        except Exception as e:
            # {"error": "Tool 'x' execution failed: ...", "type": ...}
            ...
```

`_create_cancelled_response` liefert `{"error": "Tool 'x' was cancelled.", "cancelled": true, "forced": false}` (bzw. `"was force-cancelled."`, `"forced": true`) und das Event `tool_cancelled` bzw. `tool_force_cancelled`.

Der `CancellationManager` (`agent_system.core.cancellation`) prüft periodisch die Tokens: ist nach `cancel()` die `cleanup_timeout` abgelaufen, setzt er `is_forced` und bricht alle registrierten Tasks mit passender ID bzw. Präfix per `task.cancel()` ab.

### Cancellation in Tools

`_cancellation_token` wird nur injiziert, wenn der Tool-Call eine Request-ID hat. Tools können den Status prüfen:

```python
async def long_running_tool(self, params: Dict[str, Any]) -> Any:
    """Tool that supports graceful cancellation."""
    token = params.get("_cancellation_token")
    
    for i in range(100):
        # Check if cancelled
        if token and token.is_cancelled:
            logger.info("Tool cancelled, cleaning up...")
            # Perform cleanup, dann dieselbe Form wie das Framework zurückgeben
            return {"error": "Tool 'long_running_tool' was cancelled.",
                    "cancelled": True, "forced": token.is_forced}
        
        # Do work
        await process_item(i)
    
    return {"processed": 100}
```

Kein `raise CancellationError(...)` im Tool: der Konstruktor verlangt `request_id`, und auf dem Plugin-Pfad fängt `_execute_plugin_tool` jede Exception generisch ab — das Modell bekäme nur `{"error": "<Exception-Text>"}`. Reagiert ein Tool gar nicht, bricht der Manager den Task nach `tool_cleanup_timeout` (Default 30s) ab.

## Error Handling

### Exception Types

1. **Tool nicht verfügbar**
```python
if not tool_name or tool_name not in available_tools:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=openai_tool_name,
        content=json.dumps({
            "error": f"Unknown tool: '{tool_name}'. The tool does not exist. Please check available tools and try again.",
            "type": "ToolNotFoundError"
        })
    )
```

Ungültige Argumente (kein valides JSON) liefern analog `{"error": "Invalid tool arguments for '...': ...", "type": "JSONParseError"}`; der Call wird nicht ausgeführt.

2. **Tool-Ausführungsfehler**
```python
try:
    result = await server.call_with_status(openai_tool_name, params)
except Exception as e:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=sanitize_for_llm(openai_tool_name),
        # Plugin-Tools: {"error": sanitize_for_llm(str(e))}
        # Externe Tools: {"error": f"Tool invocation failed: {str(e)}"}
        content=sanitize_json_content(json.dumps({"error": sanitize_for_llm(str(e))}))
    )
```

3. **Cancellation**
```python
if token.is_cancelled:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=sanitize_for_llm(openai_tool_name),
        content=json.dumps({
            "error": f"Tool '{tool_name}' was cancelled.",  # bzw. "was force-cancelled."
            "cancelled": True,
            "forced": forced
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
    "type": "tool_error",   # nicht "error" -- das würde das Frontend abbrechen lassen
    "tool": "unknown_tool",
    "error": "Unknown tool: unknown_tool"
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
    name=openai_tool_name,  # Erfolgspfad Plugin-Tools; Fehler-/externe Pfade: sanitize_for_llm(...)
    content=sanitize_json_content(json.dumps(result, ensure_ascii=False, default=str))
)
```

`default=str`: nicht JSON-serialisierbare Werte (z.B. `set`, `datetime`) kommen als Text beim Modell an, statt den Call scheitern zu lassen.

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
            "name": "datetime_operations",
            "arguments": '{"operation": "current"}'
        }
    }
]

# Ergebnis einsammeln (Tests: shared Helper; Produktion konsumiert den Stream)
from tool_execution_test_helpers import execute_tools_collect
messages, events, results = await execute_tools_collect(tool_execution_manager,
    tool_calls=tool_calls,
    tool_name_mapping={"datetime_operations": "datetime_operations"},
    available_tools=["datetime_operations"],
    step=0,
    request_id="req_123"
)

# Result:
# messages = [ChatMessage(role="tool", content='{...}')]
# events = [
#     {"type": "mcp_call", "server": "datetime_operations", ...},
#     {"type": "mcp_result", "server": "datetime_operations", ...}
# ]
```

### Beispiel 2: Parallele Tool-Ausführung

```python
# LLM ruft mehrere Tools gleichzeitig auf
tool_calls = [
    {"id": "call_1", "function": {"name": "datetime_operations", "arguments": '{"operation": "current"}'}},
    {"id": "call_2", "function": {"name": "tavily_search_web_search", "arguments": '{"query": "Python"}'}},
    {"id": "call_3", "function": {"name": "context7_resolve_library_id", "arguments": '{"libraryName": "fastapi"}'}}
]

# Alle Tools werden parallel ausgeführt
messages, events, results = await execute_tools_collect(tool_execution_manager,
    tool_calls=tool_calls,
    tool_name_mapping={...},
    available_tools=["datetime_operations", "tavily_search_web_search", "context7.resolve-library-id"],
    step=0,
    request_id="req_456"
)

# Jeder Tool-Call erhält eine eindeutige ID (Agent-Counter, frische Instanz):
# - datetime: req_456_001
# - tavily_search: req_456_002
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
        
        # Complete (StatusScope hat kein complete() -- end() schließt den Scope)
        if status:
            await status.end("Done!", meta={"progress": 100})
        
        return {"result": "success"}

# Client erhält Status-Events:
# {"type": "status", "phase": "start", "message": "started", ...}
# {"type": "status", "phase": "progress", "message": "Starting...", "meta": {"progress": 0}}
# {"type": "status", "phase": "progress", "message": "Processing step 1/10", "meta": {"progress": 10}}
# ...
# {"type": "status", "phase": "end", "message": "Done!", "meta": {"progress": 100}}
```

### Beispiel 5: Tool-Cancellation

```python
# Client startet Request
response = await agent.run_events(
    task="Long running task",
    request_id="req_789"
)

# Client cancelt Request (synchron; trifft auch alle Tokens mit Präfix "req_789_")
cancellation_manager.cancel_request("req_789")

# Tool prüft Cancellation
async def my_tool(self, params: Dict[str, Any]) -> Any:
    token = params.get("_cancellation_token")
    
    for i in range(1000):
        if token and token.is_cancelled:
            # Cleanup
            await cleanup()
            return {"error": "Tool 'my_tool' was cancelled.",
                    "cancelled": True, "forced": token.is_forced}
        
        await process_item(i)

# Modell erhält:
# ChatMessage(
#     role="tool",
#     content='{"error": "Tool \'my_tool\' was cancelled.", "cancelled": true, "forced": false}'
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
- **Validierung**: Validiere Parameter früh im Tool — das Framework prüft Argumente nicht gegen das JSON-Schema
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
        return {"error": "Tool 'x' was cancelled.", "cancelled": True, "forced": token.is_forced}
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

- [Plugin Architecture](_arch_plugin_architecture.md) - Plugin-System
- [MCP Configuration](mcp_configuration.md) - MCP-Server-Konfiguration
- [Status Design](status_design.md) - Status-System
- [Cancellation Architecture](cancellation_architecture.md) - Cancellation-System
- [Multimodal Tool Responses](multimodal_tool_responses_design.md) - `_multimodal_content`
