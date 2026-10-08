# Tool Execution System

Tool execution is a central part of the AgentSystem. It enables agents to use both internal plugin tools and tools of external MCP servers, and to manage their execution.

## Overview

The tool execution system comprises several components:

1. **Tool Discovery** - Detection of available tools
2. **Tool Filtering** - Application of access restrictions
3. **Tool Execution** - Parallel and sequential execution
4. **Status Management** - Status updates during execution
5. **Error Handling** - Handling of errors and aborts

## Architektur

### Main Components

```
┌─────────────────────────────────────────────────────────────┐
│                        Agent Server                          │
│  ┌───────────────────────────────────────────────────────┐  │
│  │         ToolIntegrationManager                         │  │
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
│  Plugin Tools    │              │  External Tools  │
│  (Internal)      │              │  (Remote Servers)    │
└──────────────────┘              └──────────────────────┘
```

## Tool Discovery

### 1. Tool Sources

The system collects tools from various sources:

#### Plugin Tools (Internal)
```python
# From the plugin registry
plugin_tools = await tool_integration.plugin_registry.get_all_tools()
```

Examples:
- `todo` - Task list
- `tavily_search` - Web search
- `datetime` - Date and time

#### External MCP Tools
```python
# From external MCP servers (key "external_servers")
all_tools = await tool_integration.list_all_tools()
external_tools = all_tools["external_servers"]
```

Examples:
- `context7.resolve-library-id` - Library ID resolution
- `context7.get-library-docs` - Retrieve documentation
- `memory.create_entities` - Create knowledge graph entities

#### Agent-Owned Tools
```python
# Tools defined by the agent itself
own_tools = agent.get_tools()
```

Examples:
- `basic_agent_execute_task` - Task execution (BasicAgent)
- `sysadmin_agent_execute_task` - SSH-based task execution (SysAdminAgent)

### 2. Tool Schema Generation

Tools are converted into OpenAI-compatible schemas:

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
                    "name": "context7_resolve_library_id",  # OpenAI-compatible
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

### 3. Tool Filtering

Tools are filtered via `agent_config.tools.allowed` and `agent_config.tools.blocked`, in two stages (`servers/agent/tool_schema_builder.py`): during discovery `server_matches_patterns` selects the servers or external dotted tools; after expansion into individual tools the tool filter decides; `blocked` is then applied to the individual tools. Patterns: `*`, `server/*`, `server/tool`, exact name, `ext.*` for external servers, otherwise fnmatch globs. An empty `allowed` list means: nothing allowed.

#### Example Configuration
```yaml
# config/agents/<name>.yaml
plugins:
  servers:
    research_agent:
      agent_config:
        tools:
          allowed:
            - "tavily_search/*"  # All tavily_search tools
            - "context7.*"     # All tools of the external server context7
          blocked:
            - "context7.dangerous"
```

### 4. Custom Tool Descriptions

Server instances can override the descriptions of their own tools. `self_tool_descriptions` is set on the server entry (not in `agent_config`) and is applied in `ToolServer._apply_custom_tool_descriptions` (`tools/base.py`); entries for non-existent tools are skipped with a warning.

```python
def _apply_custom_tool_descriptions(self, tools_schema: List[dict]) -> None:
    """Apply custom self tool descriptions from tool server configuration."""
    self_tool_descriptions = getattr(self.server_config, 'self_tool_descriptions', None)
    if not self_tool_descriptions:
        return
    for tool_name, new_desc in self_tool_descriptions.items():
        # unknown tool_name -> logger.warning, skip
        ...  # tool_schema["function"]["description"] = new_desc
```

#### Example
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
# The production interface is the streaming generator (abridged).
async def execute_tools_streaming(
    tool_calls: List[Dict],           # Tool calls from the LLM
    tool_name_mapping: Dict[str, str], # OpenAI names → original names
    available_tools: List[str],        # Available tools
    step: int,                         # Current step
    request_id: str | None = None,     # Request ID for tracking
    session_id: str | None = None,     # injected as _session_id
    user_id: str | None = None,        # injected as _user_id
    status_forwarder: Optional[StatusEventForwarder] = None  # per request
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute all tool calls, streaming status/tool events + final results."""
    
    # 1. Parse and validate tool calls
    valid_tool_executions = []
    for tc in tool_calls:
        openai_name = tc["function"]["name"]
        tool_name = tool_name_mapping.get(openai_name, openai_name)
        
        # Parse arguments (utils.json_utils.parse_tool_arguments). Broken
        # JSON is not repaired: error message "JSONParseError", the call
        # is NOT executed. No validation against the JSON schema.
        params, parse_problem = parse_tool_arguments(tc["function"]["arguments"])
        
        # Runtime parameters supplied by the model ("_" keys, request_id/requestId) are discarded
        params = {k: v for k, v in params.items()
                  if not k.startswith("_") and k not in ("request_id", "requestId")}
        
        if tool_name not in available_tools:
            # Error-Message "ToolNotFoundError"
            continue
        
        valid_tool_executions.append((tc, tool_name, openai_name, params))
    
    # 2. Start all tools as tasks
    tasks = []
    for i, (tc, tool_name, openai_name, params) in enumerate(valid_tool_executions):
        # Tool-specific request_id via the agent counter
        tool_request_id = await self._agent.next_internal_tool_request_id(request_id)
        params["request_id"] = params["requestId"] = tool_request_id
        tasks.append(asyncio.create_task(self._execute_single_tool(
            tc, tool_name, openai_name, params, step, tool_request_id,
            session_id, user_id, main_request_id=request_id)))
    
    # 3. Poll (asyncio.wait, 50 ms) and pass status events through meanwhile
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(pending, timeout=0.05,
                                           return_when=asyncio.FIRST_COMPLETED)
        for status_event in status_forwarder.get_pending_events():
            yield {"type": "status", "event": status_event}
        # collect finished tasks; Exception/CancelledError → error message
    
    # 4. Sort messages in call order (Gemini requires this)
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
        # With a request ID always via the cancellation path (see below)
        return await self._execute_with_cancellation(...)
    if "." in tool_name:
        # External tool
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
    
    # 1. Get the server from the registry (fallback: the agent's own tool)
    server = self._agent._get_server_from_any_registry(tool_name)
    
    # 2. Inject runtime parameters (inject_runtime_params):
    #    _session_id, _user_id, _request_id, _agent_name, _agent
    params = inject_runtime_params(params, session_id=session_id,
                                   user_id=user_id, request_id=request_id,
                                   agent=self._agent)
    
    # 3. Execute the tool (call_with_status additionally injects _status)
    result = await server.call_with_status(openai_tool_name, params)
    
    # 4. Extract _multimodal_content
    multimodal_content = result.pop("_multimodal_content", None) if isinstance(result, dict) else None
    
    # 5. Create the response message
    tool_call_id = tc.get("id") or f"{tool_name}-call-{timestamp}"
    message = ChatMessage(
        role="tool",
        tool_call_id=tool_call_id,
        name=openai_tool_name,
        content=sanitize_json_content(json.dumps(result, ensure_ascii=False, default=str)),
        multimodal_content=multimodal_content
    )
    
    # 6. Create events for streaming
    events = [
        {"type": "tool_call", "step": step + 1, "server": tool_name, "action": openai_tool_name, ...},
        {"type": "tool_result", "step": step + 1, "server": tool_name, "result": result, ...}
    ]
    
    return message, events, [{"server": tool_name, "action": openai_tool_name, "result": result, ...}]
```

Besides the model arguments, a plugin tool thus sees (each only if the value is set): `request_id`/`requestId`, `_request_id`, `_session_id`, `_user_id`, `_agent_name`, `_agent`, `_status` and (if a request ID is present) `_cancellation_token`. `_` keys supplied by the model as well as `request_id`/`requestId` are discarded beforehand: the request ID belongs to the framework, otherwise cancellation and status routing would run on the model's value.

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
    """Execute an external tool."""
    
    # 1. Parse server name and tool name
    server_name, actual_tool_name = tool_name.split(".", 1)
    # e.g. "context7.resolve-library-id" → "context7", "resolve-library-id"
    
    # 2. Call external server via the agent's tool integration.
    #    Only JSON-serializable parameters without "_" keys leave the
    #    process -- no runtime parameters, no status object, no token.
    serializable_params = self._make_params_serializable(params)
    tool_integration = self._agent._tool_integration_manager.tool_integration
    result = await tool_integration.call_tool(
        server_name,
        actual_tool_name,
        serializable_params,
        "external"
    )
    
    # 3. Create the response (analogous to plugin tools, incl. _multimodal_content)
    message = ChatMessage(...)
    events = [
        {"type": "tool_call", "step": step + 1, "server": tool_name, "action": actual_tool_name, ...},
        {"type": "tool_result", "step": step + 1, "server": tool_name, "result": result, ...}
    ]
    
    return message, events, [{"server": tool_name, "action": actual_tool_name, "result": result, ...}]
```

## Parallel Execution

### Request ID Management

Every tool call receives a unique request ID for tracking and cancellation:

```python
# Main request: "abc123"
# Tool calls receive suffixes:
# - "abc123_001" (first tool call)
# - "abc123_002" (second tool call)
# - "abc123_003" (third tool call)

for i, (tc, tool_name, openai_name, params) in enumerate(valid_tool_executions):
    tool_request_id = await agent.next_internal_tool_request_id(original_request_id)
    # → counter per agent instance (lock-protected), keeps counting across steps;
    #   without an agent: f"{original_request_id}_{i+1:03d}"
    
    params_with_id = params.copy()
    params_with_id["request_id"] = tool_request_id
    params_with_id["requestId"] = tool_request_id  # JS compatibility
```

### Parallel Tasks

Tools run in parallel as `asyncio.create_task()`; the loop waits with `asyncio.wait(..., timeout=0.05, return_when=FIRST_COMPLETED)` so that status events (e.g. from sub-agents) are already streamed during execution. There is no iteration limit.

```python
pending = set(tasks)
while pending:
    done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
    # Pass status events through ...
    for task in done:
        try:
            tool_message, events, tool_results = task.result()
        except asyncio.CancelledError:
            # {"error": "Tool 'x' was cancelled."} + Event tool_cancelled
        except Exception as e:
            # {"error": "Tool 'x' execution failed: ...", "type": ...} + Event tool_error

# Then sort by original index -- Gemini requires
# function_response in the order of the function_calls.
```

## Status Management

### StatusScope Integration

Tools automatically receive a `StatusScope` object for updates:

```python
async def call_with_status(self, action: str, params: dict[str, Any]):
    """Call tool with automatic StatusScope management."""
    from .status import get_status_bus, status_scope
    
    status_bus = get_status_bus()
    request_id = params.get("request_id") or params.get("requestId")
    
    # Scope name: "<server>.<method>()", e.g. "my_plugin_path_validate" -> "my_plugin.path_validate()"
    method_name = action.replace(f"{self.name}_", "") if action.startswith(f"{self.name}_") else action
    scope_name = f"{self.name}.{method_name}()"
    
    async with status_scope(status_bus, scope_name, request_id=request_id) as status:
        # Inject status object for the plugin to use
        params_with_status = params.copy()
        params_with_status["_status"] = status
        if request_id:
            params_with_status["_request_id"] = request_id
        
        result = await self.call(action, params_with_status)
        # Safety net: if the handler returns an error result without
        # reporting status.error/end itself, call_with_status sends
        # status.error(...) instead of END "completed".
        return result
```

Without an explicit call to `end()`/`error()`, the scope automatically sends `END` ("completed") on exit or, on an exception, `ERROR` ("failed: …").

### Status Updates in Tools

Tools can send status updates:

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

`StatusScope` only knows `progress()`, `end()` and `error()`. Status events are streamed to the client via the `StatusEventForwarder` (filter: same request ID or prefix `<request_id>_`):

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
    request_id: str,                  # tool-specific ID ("abc123_001")
    session_id: str | None = None,
    user_id: str | None = None,
    main_request_id: str | None = None  # root ID ("abc123")
) -> tuple[ChatMessage, List[Dict], List[Dict]]:
    """Execute tool with cancellation support."""
    
    cancellation_manager = get_cancellation_manager()
    
    # 1. Check main request cancellation -- the token lives under the root ID
    main_token = cancellation_manager.get_token(main_request_id or request_id)
    if main_token and main_token.is_cancelled:
        return self._create_cancelled_response(tc, tool_name, ..., forced=main_token.is_forced)
    
    # 2. Create tool-specific cancellation context
    #    cleanup_timeout from agent_config.timeouts.tool_cleanup_timeout (default 30s)
    tool_request_id = f"{request_id}_{step:03d}"
    async with cancellable_operation(tool_request_id, cleanup_timeout=cleanup_timeout) as tool_token:
        
        # 3. Add cancellation token to params
        params_with_token = params.copy()
        params_with_token["_cancellation_token"] = tool_token
        
        # 4. Execute tool (_execute_plugin_tool or _execute_external_tool)
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

`_create_cancelled_response` returns `{"error": "Tool 'x' was cancelled.", "cancelled": true, "forced": false}` (or `"was force-cancelled."`, `"forced": true`) and the event `tool_cancelled` or `tool_force_cancelled`.

The `CancellationManager` (`agent_system.core.cancellation`) periodically checks the tokens: once the `cleanup_timeout` has elapsed after `cancel()`, it sets `is_forced` and aborts all registered tasks with a matching ID or prefix via `task.cancel()`.

### Cancellation in Tools

`_cancellation_token` is only injected if the tool call has a request ID. Tools can check the status:

```python
async def long_running_tool(self, params: Dict[str, Any]) -> Any:
    """Tool that supports graceful cancellation."""
    token = params.get("_cancellation_token")
    
    for i in range(100):
        # Check if cancelled
        if token and token.is_cancelled:
            logger.info("Tool cancelled, cleaning up...")
            # Perform cleanup, then return the same shape as the framework
            return {"error": "Tool 'long_running_tool' was cancelled.",
                    "cancelled": True, "forced": token.is_forced}
        
        # Do work
        await process_item(i)
    
    return {"processed": 100}
```

No `raise CancellationError(...)` in the tool: the constructor requires `request_id`, and on the plugin path `_execute_plugin_tool` catches every exception generically — the model would only get `{"error": "<exception text>"}`. If a tool does not react at all, the manager aborts the task after `tool_cleanup_timeout` (default 30s).

## Error Handling

### Exception Types

1. **Tool not available**
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

Invalid arguments (not valid JSON) analogously yield `{"error": "Invalid tool arguments for '...': ...", "type": "JSONParseError"}`; the call is not executed.

Before this check, `execute_tools_streaming` asks the optional `intercept`
(deferred tools, `docs/deferred_tools.md`). It answers `tool_search`
itself, and it answers a deferred tool that was called before being loaded
with `{"type": "ToolNotLoaded", "error": ...}` without executing it. None of
these calls reaches a hook, and `ToolNotLoaded`, like a blocked
call, does not count toward the error streak of auto-escalation.

2. **Tool execution error**
```python
try:
    result = await server.call_with_status(openai_tool_name, params)
except Exception as e:
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=sanitize_for_llm(openai_tool_name),
        # Plugin-Tools: {"error": sanitize_for_llm(str(e))}
        # External tools: {"error": f"Tool invocation failed: {str(e)}"}
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
            "error": f"Tool '{tool_name}' was cancelled.",  # or "was force-cancelled."
            "cancelled": True,
            "forced": forced
        })
    )
```

4. **GeneratorExit** (async generator tools)
```python
except GeneratorExit:
    # Tool was closed prematurely
    error_message = ChatMessage(
        role="tool",
        tool_call_id=tc.get("id"),
        name=openai_tool_name,
        content=json.dumps({"error": "Tool execution was cancelled (GeneratorExit)"})
    )
```

### Error Events

Errors are streamed as events:

```python
{
    "type": "tool_error",   # not "error" -- that would make the frontend abort
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

### LLM-Safe Output

Tool results are sanitized before being passed on to the LLM:

```python
from agent_system.llm.text_sanitizer import sanitize_for_llm, sanitize_json_content

# Tool name sanitization
message = ChatMessage(
    role="tool",
    tool_call_id=tc.get("id"),
    name=openai_tool_name,  # success path of plugin tools; error/external paths: sanitize_for_llm(...)
    content=sanitize_json_content(json.dumps(result, ensure_ascii=False, default=str))
)
```

`default=str`: values that are not JSON-serializable (e.g. `set`, `datetime`) arrive at the model as text instead of making the call fail.

### Serialization for Events

Events contain only JSON-serializable data:

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

## Examples

### Example 1: Simple Tool Execution

```python
# LLM calls a tool
tool_calls = [
    {
        "id": "call_abc123",
        "function": {
            "name": "datetime_operations",
            "arguments": '{"operation": "current"}'
        }
    }
]

# Collect the result (tests: shared helper; production consumes the stream)
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
#     {"type": "tool_call", "server": "datetime_operations", ...},
#     {"type": "tool_result", "server": "datetime_operations", ...}
# ]
```

### Example 2: Parallel Tool Execution

```python
# LLM calls several tools at once
tool_calls = [
    {"id": "call_1", "function": {"name": "datetime_operations", "arguments": '{"operation": "current"}'}},
    {"id": "call_2", "function": {"name": "tavily_search_web_search", "arguments": '{"query": "Python"}'}},
    {"id": "call_3", "function": {"name": "context7_resolve_library_id", "arguments": '{"libraryName": "fastapi"}'}}
]

# All tools are executed in parallel
messages, events, results = await execute_tools_collect(tool_execution_manager,
    tool_calls=tool_calls,
    tool_name_mapping={...},
    available_tools=["datetime_operations", "tavily_search_web_search", "context7.resolve-library-id"],
    step=0,
    request_id="req_456"
)

# Each tool call receives a unique ID (agent counter, fresh instance):
# - datetime: req_456_001
# - tavily_search: req_456_002
# - context7: req_456_003
```

### Example 3: External MCP Tool

```python
# LLM calls an external tool
tool_calls = [
    {
        "id": "call_ext",
        "function": {
            "name": "context7_get_library_docs",  # OpenAI-compatible
            "arguments": '{"context7CompatibleLibraryID": "/fastapi/fastapi"}'
        }
    }
]

# Tool name mapping
tool_name_mapping = {
    "context7_get_library_docs": "context7.get-library-docs"  # Original name
}

# Execution via tool integration
messages, events, results = await execute_tools_collect(tool_execution_manager, ...)

# Internally this calls:
# tool_integration.call_tool(
#     server_name="context7",
#     tool_name="get-library-docs",
#     params={"context7CompatibleLibraryID": "/fastapi/fastapi"},
#     category="external"
# )
```

### Example 4: Tool with Status Updates

```python
# Plugin tool with status support
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
        
        # Complete (StatusScope has no complete() -- end() closes the scope)
        if status:
            await status.end("Done!", meta={"progress": 100})
        
        return {"result": "success"}

# Client receives status events:
# {"type": "status", "phase": "start", "message": "started", ...}
# {"type": "status", "phase": "progress", "message": "Starting...", "meta": {"progress": 0}}
# {"type": "status", "phase": "progress", "message": "Processing step 1/10", "meta": {"progress": 10}}
# ...
# {"type": "status", "phase": "end", "message": "Done!", "meta": {"progress": 100}}
```

### Example 5: Tool Cancellation

```python
# Client starts a request
response = await agent.run_events(
    task="Long running task",
    request_id="req_789"
)

# Client cancels the request (synchronous; also hits all tokens with prefix "req_789_")
cancellation_manager.cancel_request("req_789")

# Tool checks cancellation
async def my_tool(self, params: Dict[str, Any]) -> Any:
    token = params.get("_cancellation_token")
    
    for i in range(1000):
        if token and token.is_cancelled:
            # Cleanup
            await cleanup()
            return {"error": "Tool 'my_tool' was cancelled.",
                    "cancelled": True, "forced": token.is_forced}
        
        await process_item(i)

# Model receives:
# ChatMessage(
#     role="tool",
#     content='{"error": "Tool \'my_tool\' was cancelled.", "cancelled": true, "forced": false}'
# )
```

## Best Practices

### 1. Tool Design

- **Idempotence**: Tools should be idempotent (repeated calls with the same parameters = same result)
- **Error Handling**: Always return meaningful error messages
- **Status Updates**: Send status updates regularly during long operations
- **Cancellation**: Check the cancellation token during long-running operations

### 2. Parameter Design

- **JSON-serializable**: All parameters must be JSON-serializable
- **Clear names**: Use descriptive parameter names
- **Validation**: Validate parameters early in the tool — the framework does not check arguments against the JSON schema
- **Defaults**: Offer sensible default values

### 3. Performance

- **Parallel Execution**: Use parallel execution where possible
- **Timeouts**: Set timeouts for external calls
- **Caching**: Cache expensive operations
- **Streaming**: Stream large amounts of data instead of loading them completely

### 4. Monitoring

- **Request IDs**: Use request IDs for tracking
- **Logging**: Log important events (start, end, errors)
- **Metrics**: Track execution times and error rates
- **Events**: Emit events for important state changes

## Troubleshooting

### Problem: Tool Not Found

**Symptom**: `"Unknown tool: <tool_name>"`

**Causes**:
1. Tool is not in the `available_tools` list
2. Tool was blocked by a filter
3. Tool name mapping is wrong

**Solution**:
```python
# Debug available tools
logger.debug(f"Available tools: {available_tools}")
logger.debug(f"Tool name mapping: {tool_name_mapping}")

# Check filter configuration
agent.agent_config.tools  # Allowed/blocked patterns
```

### Problem: Tool Execution Hangs

**Symptom**: Tool no longer responds

**Causes**:
1. Tool is waiting for input/lock
2. Deadlock in async code
3. Missing timeouts

**Solution**:
```python
# Implement timeouts
try:
    result = await asyncio.wait_for(
        server.call(tool_name, params),
        timeout=30.0
    )
except asyncio.TimeoutError:
    raise RuntimeError("Tool execution timeout")

# Check cancellation regularly
for i in range(1000):
    if token and token.is_cancelled:
        return {"error": "Tool 'x' was cancelled.", "cancelled": True, "forced": token.is_forced}
    await process_item(i)
```

### Problem: Status Updates Do Not Arrive

**Symptom**: No status events in the client

**Causes**:
1. `_status` not injected into params
2. `call_with_status()` not used
3. Status bus not configured

**Solution**:
```python
# Always use call_with_status
result = await server.call_with_status(tool_name, params)

# Check the status object in the tool
status = params.get("_status")
if status:
    await status.progress("Update message")
else:
    logger.warning("No status object available")
```

### Problem: Request ID Collisions

**Symptom**: Events are assigned to the wrong requests

**Causes**:
1. Request IDs are not unique
2. Tool-specific suffixes are missing

**Solution**:
```python
# Use the agent counter for unique IDs
tool_request_id = await agent.next_internal_tool_request_id(request_id)

# Or manual suffixes
tool_request_id = f"{request_id}_{step:03d}_{i:03d}"
```

## See Also

- [Plugin Architecture](_arch_plugin_architecture.md) - Plugin system
- [Tool server configuration](configuration.md) - Tool server configuration
- [Cancellation Architecture](cancellation_architecture.md) - Cancellation system
