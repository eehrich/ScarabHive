# Plugin Hook System

The verified quick reference is the Claude skill `.claude/skills/plugin-authoring/` (`references/hooks.md`).

**Status:** Production Ready
**Version:** 1.0
**Last Updated:** 2025-10-14

## Table of Contents

1. [Overview](#overview)
2. [Hook Types](#hook-types)
3. [PluginHook Interface](#pluginhook-interface)
4. [Hook Ordering](#hook-ordering)
5. [Schema-Based Hooks](#schema-based-hooks)
6. [Configuration](#configuration)
7. [Best Practices](#best-practices)
   - [Cache Safety](#cache-safety)
8. [Examples](#examples)
9. [Troubleshooting](#troubleshooting)

## Overview

The Plugin Hook System provides lifecycle interception points for extending agent behavior without modifying core code. Hooks allow plugins to observe and modify agent operations at key points in the execution flow.

### Key Features

- **10 Hook Types**: session start/end, pre/post LLM call, LLM progress, format output, pre LLM request/post LLM response (client level), pre/post tool call (defined but never fired)
- **Flexible Ordering**: Named dependencies with topological sorting
- **Schema-Based Pattern**: Declarative hook definitions in YAML
- **Error Isolation**: Hook failures don't crash the agent
- **Global Configuration**: Override hook behavior per agent/environment
- **Metadata Tracking**: Detailed execution statistics and audit trails

### Architecture

```
Agent Execution Flow
  ↓
  SESSION_START hooks (new sessions only)
  ↓
  ┌─ PRE_LLM_CALL hooks (every step)
  │    ↓
  │  LLM call ── PRE_LLM_REQUEST / LLM_PROGRESS / POST_LLM_RESPONSE (client level)
  │    ↓
  │  POST_LLM_CALL hooks
  │    ↓
  │  FORMAT_OUTPUT hooks (display)
  │    ↓
  └─ Tool execution (PRE_TOOL_CALL / POST_TOOL_CALL are not fired)
  ↓
  Session saved
  ↓
  SESSION_END hooks
```

### What Takes Effect

The registry hands every hook a **deep copy** of the context (`messages`,
`llm_response`, `metadata`, ...; `agent`, `llm`, `tools_schema` and the
cancellation token by reference).

- Changes are only taken when the hook returns `success=True, modified=True`
  with the changed `context`; otherwise they are discarded.
- `result.metadata` is merged into the context metadata even with
  `modified=False`.
- `success=False` → neither context nor metadata is taken.

### Use Cases

- **Context Management**: Optimize message history before LLM calls
- **Validation**: Validate message format and content
- **Logging**: Audit agent behavior and track metrics
- **Transformation**: Modify inputs/outputs dynamically
- **Security**: Sanitize content, enforce policies
- **Monitoring**: Track performance, costs, usage patterns

## Hook Types

### PRE_LLM_CALL

**Trigger:** Every step before the LLM call, including the final call after `max_steps`
**Use Cases:** Context optimization, prompt injection, validation
**Can Modify:** `messages` — the changed list is sent as-is and synced to the session tracker.
The context also carries `tools_schema`, `llm`, `step` and `cancellation_token`.

```python
async def on_pre_llm_call(self, context: HookContext) -> HookResult:
    """Executed before LLM call.

    Common use cases:
    - Optimize context (remove duplicates, truncate)
    - Inject system prompts
    - Validate message structure
    - Add custom metadata
    """
```

**Example Plugins:**
- `context_engineer`: Kompaktiert den Kontext in Schichten (Tool-Ergebnisse auslagern, archivieren)
- `context_summarizer`: Intelligently summarizes older messages
- `message_validator`: Validates message format

### POST_LLM_CALL

**Trigger:** After receiving the LLM response
**Use Cases:** Response validation, logging, statistics, auto-continuation
**Can Modify:** Only `llm_response["assistant"]["content"]` and
`llm_response["assistant"]["tool_calls"]` are read back. Metadata keys the
loop reads: `content_format`, `continue`, `continue_message`,
`continue_injected_by`, `continuation_count`, `continuation_reason`.
A hook that sets `continue` should also set `continue_injected_by`, so it can
count its own nudges; without one the loop marks the nudge `post_llm_call_hook`
(see [Cache Safety](#cache-safety)).

```python
async def on_post_llm_call(self, context: HookContext) -> HookResult:
    """Executed after LLM call.

    Common use cases:
    - Log response and timing
    - Validate response format
    - Extract structured data
    - Update statistics
    """
```

**Example Plugins:**
- `request_logger`: Logs timing and response preview

### LLM_PROGRESS

**Trigger:** Während eines streamenden LLM-Calls, alle 2000 Zeichen Denken
(`_REASONING_PROGRESS_TICK` in `server.py`)
**Use Cases:** einen Call beobachten, der sich festdenkt
**Can Modify:** nichts — der Call läuft schon

```python
async def on_llm_progress(self, context: HookContext) -> HookResult:
    """context.reasoning_text: das Denken dieses Calls bisher,
    context.reasoning_chars / previous_reasoning_chars: Länge jetzt und beim
    letzten Takt. KEINE messages."""
```

- **Der Stream wartet auf den Hook** — er muss sofort zurückkehren; alles
  Langsame gehört in einen Hintergrund-Task. Fehler im Hook brechen den Call
  nicht ab.
- **Keine `messages`:** der Registry-Deep-Copy pro Takt blockiert den
  streamenden Loop (gemessen: 72 ms bei 4691 Nachrichten). Wer den Auftrag
  braucht, merkt ihn sich in einem `pre_llm_call`-Hook.
- **Eigenes Intervall ohne Zustand:** fällig, wenn
  `reasoning_chars // n > previous_reasoning_chars // n`.
- **Blind**, wo kein `thinking_delta` fließt: Gemini (Gedanken kommen als
  `content_delta`), Batch, nicht-streamende Clients.

**Example Plugins:**
- `agent_watchdog`: `observe_reasoning`

### PRE_TOOL_CALL

> ⚠️ **Never fires:** The hook type, registry routing and
> `HookIntegrationManager.execute_pre_tool_hooks` / `execute_post_tool_hooks`
> exist, but nothing calls them — hooks of type PRE_TOOL_CALL and
> POST_TOOL_CALL **never fire**. Registering one logs a warning. Do not build
> plugins on them.

**Trigger:** none (intended: before executing a tool)
**Intended Use Cases:** Parameter validation, access control, logging

```python
async def on_pre_tool_call(self, context: HookContext) -> HookResult:
    """Executed before tool call.

    Common use cases:
    - Validate tool parameters
    - Check access permissions
    - Log tool invocation
    - Modify parameters
    """
```

### POST_TOOL_CALL

> ⚠️ **Never fires** — see PRE_TOOL_CALL.

**Trigger:** none (intended: after tool execution)
**Intended Use Cases:** Result validation, error handling, logging

```python
async def on_post_tool_call(self, context: HookContext) -> HookResult:
    """Executed after tool call.

    Common use cases:
    - Validate tool results
    - Log execution time
    - Handle errors gracefully
    - Transform results
    """
```

### FORMAT_OUTPUT

**Trigger:** Before returning output to the user
**Use Cases:** Multi-format rendering (HTML, ANSI, text), filtering
**Can Modify:** Displayed output only — never the conversation history

```python
async def on_format_output(self, context: HookContext) -> HookResult:
    """Executed before output formatting.

    Context provides:
    - output: The content to format (usually Markdown)
    - output_format: Target format ('html', 'ansi', 'text', 'markdown')

    Common use cases:
    - Convert markdown to HTML (web frontend with syntax highlighting)
    - Convert markdown to ANSI (CLI with colors)
    - Apply custom formatting per interface
    - Filter sensitive information
    - Add metadata/footers

    Example:
        target_format = context.output_format or 'text'

        if target_format == 'html':
            # Convert to HTML with Prism.js syntax highlighting
            html = markdown_to_html(context.output)
            return HookResult(
                success=True,
                modified=True,
                context=replace(context, output=html),
                metadata={'content_format': 'html'}
            )
        elif target_format == 'ansi':
            # Convert to ANSI colored terminal output
            ansi = markdown_to_ansi(context.output)
            return HookResult(
                success=True,
                modified=True,
                context=replace(context, output=ansi),
                metadata={'content_format': 'ansi'}
            )
        else:
            # Return plain text/markdown unchanged
            return HookResult(success=True, modified=False, context=context)
    """
```

### SESSION_START

**Trigger:** When a new session begins (not for resumed sessions)
**Use Cases:** Initialization, logging, setup
**Can Modify:** `messages` — the returned list replaces the session's message list

```python
async def on_session_start(self, context: HookContext) -> HookResult:
    """Executed at session start.

    Common use cases:
    - Initialize session tracking
    - Log session start
    - Set up session state
    - Load session context
    """
```

**Example Plugins:**
- `request_logger`: Initializes session tracking

### SESSION_END

**Trigger:** When the request ends
**Use Cases:** Cleanup, statistics, logging
**Can Modify:** nothing — the return value is ignored

**When:** after the request's conversation has been written to the session
file, not before. `context.metadata["persisted"]` says whether that write
happened -- a hook that counts what the request carried as done (debate_forum
marks direct messages delivered there) may only do so when it did: persisting
never raises, so a failed or cancelled save is silent otherwise.

```python
async def on_session_end(self, context: HookContext) -> HookResult:
    """Executed at session end.

    Common use cases:
    - Clean up resources
    - Log session summary
    - Save session statistics
    - Final reporting
    """
```

**Example Plugins:**
- `request_logger`: Logs session duration and statistics

### PRE_LLM_REQUEST / POST_LLM_RESPONSE

**Trigger:** Inside the LLM client, around the raw API request
**Use Cases:** Capturing exact payloads, responses, usage
**Can Modify:** nothing — read-only; errors in these hooks are swallowed

Fields: `llm_request_payload`, `llm_response_data`, `llm_provider`,
`llm_model`, `llm_request_url`, `llm_duration_ms`, `llm_error`, `llm_usage`,
`llm_finish_reason`, `llm_is_streaming`.

## PluginHook Interface

### Base Class

All hook plugins inherit from `PluginHook` or `SchemaBasedPluginHook`:

```python
from agent_system.hooks import PluginHook, HookContext, HookResult

class MyPlugin(PluginHook):
    def __init__(self, name: str, config: dict = None):
        super().__init__(name, config or {})
        # Plugin initialization

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # Hook implementation
        return HookResult(
            success=True,
            modified=False,
            context=context
        )
```

### HookContext

Container for hook execution context:

```python
@dataclass
class HookContext:
    hook_type: HookType
    request_id: str
    session_id: str
    agent: Optional[Agent] = None
    agent_name: str = ""
    messages: Optional[List[ChatMessage]] = None          # ChatMessage objects, not dicts
    tools_schema: Optional[List[Dict[str, Any]]] = None   # per-request tool schema
    llm_response: Optional[Dict[str, Any]] = None         # post_llm_call: {"assistant": {...}}
    tool_call: Optional[Dict[str, Any]] = None            # tool hooks (never fired)
    tool_result: Optional[Dict[str, Any]] = None          # tool hooks (never fired)
    output: Optional[str] = None                          # format_output
    output_format: str = "text"                           # 'html', 'ansi', 'text', 'markdown'
    metadata: Dict[str, Any] = field(default_factory=dict)
    hook_config: Dict[str, Any] = field(default_factory=dict)  # per-agent override keys
    target_hook_name: Optional[str] = None                # short hook name being dispatched
    step: int = 0
    llm: Optional[Any] = None
    cancellation_token: Optional[Any] = None
    # llm_request_payload, llm_response_data, llm_provider, llm_model,
    # llm_request_url, llm_duration_ms, llm_error, llm_usage,
    # llm_finish_reason, llm_is_streaming: pre_llm_request / post_llm_response
    # reasoning_text, reasoning_chars, previous_reasoning_chars: llm_progress
```

### HookResult

Return value from hook execution:

```python
@dataclass
class HookResult:
    success: bool                                          # Hook executed successfully
    modified: bool = False                                 # Context was modified
    context: Optional[HookContext] = None                  # Modified (or original) context
    error: Optional[str] = None                            # Error message if failed
    metadata: Dict[str, Any] = field(default_factory=dict) # Merged into context.metadata
```

**Important:**
- Set `modified=True` if you changed the context — otherwise the change is discarded
- Return modified context in `context` field
- Use `metadata` for signals and statistics; it is merged even with `modified=False`
- Set `success=False` and provide `error` on failure — nothing is taken then

## Hook Ordering

### Named Dependencies

Hooks specify dependencies in `order`:

```yaml
hooks:
  - name: my_hook
    type: pre_llm_call
    category: prompt_injection
    order:
      after: ["begin", "context_engineer.engineer_context"]  # full name
      before: ["context_summarization", "end"]               # category or virtual node
```

A reference resolves only to:

- a **full hook name** (`<server instance name>.<hook name>`),
- a **category** (all registered hooks with that `category`),
- `begin` / `end`: virtual nodes at the start/end of each type.

A short hook name (`after: ["engineer_context"]`) or a reference to a hook
that is not registered is **silently ignored** (debug log only).

### Topological Sort

The `HookRegistry` sorts with Kahn's algorithm on every execution:

1. Resolve references and build the dependency graph
2. Sort topologically; ties are broken alphabetically by full name
3. On a circular dependency: an error is logged and the hooks run in
   registration order — nothing is raised

### Example Ordering

```yaml
# config/plugins.yaml
hooks:
  enabled: true
  overrides:
    request_logger.log_pre_llm:
      order:
        after: ["begin"]
        before: ["context_summarizer.summarize_context"]

    context_summarizer.summarize_context:
      order:
        after: ["request_logger.log_pre_llm"]
        before: ["message_validator.validate_messages"]

    message_validator.validate_messages:
      order:
        after: ["context_summarizer.summarize_context"]
        before: ["end"]
```

**Execution Order:**
1. `request_logger.log_pre_llm`
2. `context_summarizer.summarize_context`
3. `message_validator.validate_messages`

## Schema-Based Hooks

Recommended pattern for new hook plugins.

### Directory Structure

```
src/plugins/my_plugin/
├── plugin.toml      # Manifest (entrypoint = "plugin:PLUGIN_FACTORY")
├── plugin.py        # Factory function
├── hooks.py         # Hook implementation
├── schema.yaml      # Hook definitions + config
└── README.md        # Documentation
```

### schema.yaml

```yaml
# Hook definitions
hooks:
  - name: my_hook_handler        # Must match method name exactly (handler:/priority: are ignored)
    type: pre_llm_call
    enabled: true
    timeout: 30.0                # default 30.0
    category: prompt_injection   # optional; order may reference categories
    description: "What this hook does"
    order:
      after: ["begin"]
      before: ["end"]

# Configuration schema
config:
  max_items:
    type: integer
    default: 100
    description: "Maximum items to process"
    minimum: 1
    maximum: 1000

  enable_feature:
    type: boolean
    default: true
    description: "Enable special feature"
```


### Instance Default via `hook_config`

The schema speaks for the plugin type. When the same plugin runs as several
instances (e.g. `context_summarizer` and `writer_context_summarizer`), an
instance's server config sets its own registration default:

```yaml
# config/plugins.yaml or an included plugin config
servers:
  writer_context_summarizer:
    type: context_summarizer
    hook_config:
      enabled: false   # this instance starts OFF; agents switch it on
                       # via hooks.overrides (exact <instance>.<hook> key)
```

It can **only disable**: `enabled: false` switches this instance's hooks off;
`enabled: true` does NOT raise a schema default (raising a default is the
operator's move — global `hooks.overrides`). Precedence at registration:
schema entry < instance `hook_config.enabled: false` < global
`hooks.overrides` < global master switch `hooks.enabled: false`. At runtime
the agent override (exact key) wins over all of these, see
[Agent-Level Config](#agent-level-config).

A hook is registered only if the plugin's `schema.yaml` has a `hooks:` key and
the instance is enabled in `config/plugins.yaml`.

### hooks.py

```python
from pathlib import Path
from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

class MyPlugin(SchemaBasedPluginHook):
    def __init__(self, plugin_dir: Path | str, mcp_config=None):
        super().__init__(plugin_dir)

        # get_config() holds only the schema.yaml defaults: {key: default}
        config = dict(self.get_config())
        # Merging the instance config from plugins.yaml is the plugin's job
        if mcp_config is not None and getattr(mcp_config, 'config', None):
            config.update(mcp_config.config)
        self.max_items = config.get('max_items', 100)

    # Handler name MUST match hook name in schema.yaml
    async def my_hook_handler(self, context: HookContext) -> HookResult:
        # Implementation
        return HookResult(
            success=True,
            modified=False,
            context=context
        )
```

### plugin.py

```python
from pathlib import Path
from .hooks import MyPlugin

# Called as factory(name, system_config, mcp_config) — a zero-argument factory raises TypeError
def PLUGIN_FACTORY(name=None, system_config=None, mcp_config=None) -> MyPlugin:
    return MyPlugin(Path(__file__).parent, mcp_config)
```

### Convention

**Hook name = Method name** (no `handler` field needed)

```yaml
# schema.yaml
hooks:
  - name: optimize_context    # Method: async def optimize_context(...)
  - name: validate_messages   # Method: async def validate_messages(...)
```

## Configuration

### Hook Naming Convention

**CRITICAL:** Hooks are registered and referenced using the **full name** format:
`<server instance name>.<hook name>` — the instance key under `servers:` in
`config/plugins.yaml`, not the plugin type or folder name.

**Examples:**
- `todo.inject_todo_tasks`
- `markdown_formatter.format_markdown_output`
- `context_engineer.engineer_context`

**Why This Matters:**
- Enables multiple plugins (and instances of one plugin) to have hooks with the same base name
- Required for global and per-agent hook overrides and for `order` references
- The registration log shows the exact name: `Registered hook '<full name>' ...`

**Convention Breakdown:**
```yaml
# In plugin schema.yaml (just the hook name)
hooks:
  - name: inject_todo_tasks
    type: pre_llm_call
    enabled: false

# In registry (full name with instance prefix)
# Registered as: todo.inject_todo_tasks

# In agent config overrides (full name required)
agent_config:
  hooks:
    overrides:
      todo.inject_todo_tasks:
        enabled: true
```

### Plugin-Level Config

Defined in plugin's `schema.yaml`:

```yaml
config:
  param1:
    type: string
    default: "value"
    description: "Parameter description"
```

### Global Overrides

Override in `config/plugins.yaml`:

```yaml
hooks:
  enabled: true                   # false disables all hooks at registration
  overrides:
    instance_name.hook_name:      # or just instance_name for all its hooks
      enabled: false              # Disable specific hook
      timeout: 60.0               # Override timeout
      order:
        after: ["other_instance.other_hook"]  # Override order (full names/categories)
```

`hooks.default_timeout` is the timeout of every hook whose schema sets no
`timeout`. Only the field names of an override are validated; a key that matches
no registered hook (or hook-owning instance) has no effect, and startup logs a
warning for it.

Ein globaler Override kennt genau diese drei Keys (`enabled`, `timeout`,
`order`) — ein anderer Key (Tippfehler wie `timout`) lässt `load_settings`
scheitern, statt still wirkungslos zu bleiben. Ein leerer Schlüssel (alle
Zeilen darunter auskommentiert) heißt „nichts gesetzt“. Plugin-spezifische
Parameter gehören in die Hook-Metadaten des Plugins bzw. die Agent-Overrides
(`agent_config.hooks.overrides`), nicht hierher.

Der `hooks:`-Abschnitt wird wie `plugins:` beim Laden der Config
zusammengeführt (`AgentSystemConfig.hooks`) — aus `config/plugins.yaml` oder
jeder anderen per `includes` geladenen Datei, und zwar der Config, mit der der
Prozess läuft (`--config`, `AGENT_CONFIG_PATH`), unabhängig vom
Arbeitsverzeichnis.

### Agent-Level Config

Per-agent hook configuration with overrides:

```yaml
# config/plugins.yaml
agents:
  meta_agent:
    agent_config:
      hooks:
        enabled: true   # false disables all hooks for this agent
        overrides:
          # Enable globally disabled hook for this agent
          todo.inject_todo_tasks:
            enabled: true
            # Any other key arrives as context.hook_config; it only has an
            # effect if the hook reads it (todo does not)

          # Disable globally enabled hook for this agent
          markdown_formatter.format_markdown_output:
            enabled: false

  simple_agent:
    agent_config:
      hooks:
        enabled: true
        overrides: {}  # Uses all global defaults
```

**Override Logic:**
1. **Agent `hooks.enabled: false`** → no hook runs for this agent
2. **Hook has agent override with `enabled`** → Use override value (ignores registration state)
3. **No agent override** → Use the registration state (schema, instance `hook_config`, global overrides)
4. **Hook globally disabled + agent enables** → Hook executes for this agent only
5. **Hook globally enabled + agent disables** → Hook skipped for this agent only

⚠️ Agent override keys must be the **full** hook name: a short name, a type
name, an instance name or a typo has no effect. Startup logs a warning for every
such key once all hooks are registered. `timeout` and `order` in agent overrides
are ignored.

**Example Scenario:**

```yaml
# schema.yaml (global)
hooks:
  - name: inject_todo_tasks
    enabled: false  # Disabled by default

# config/plugins.yaml
meta_agent:
  hooks:
    overrides:
      todo.inject_todo_tasks:
        enabled: true  # Enabled ONLY for meta_agent

sysadmin_agent:
  hooks:
    overrides: {}  # No override, uses global (disabled)
```

**Result:**
- ✅ `meta_agent`: Hook executes (override enabled)
- ❌ `sysadmin_agent`: Hook skipped (global disabled, no override)

## Best Practices

### Performance

1. **Keep Hooks Fast**: Target <100ms execution time
2. **Async Operations**: Use `await` for I/O operations
3. **Avoid Blocking**: Don't use `time.sleep()`, use `asyncio.sleep()`
4. **Cache Results**: Store expensive computations
5. **Limit Processing**: Process only what's necessary

### Error Handling

1. **Always Return HookResult**: Even on error
2. **Set success=False on Failure**: Provide error message
3. **Don't Raise Exceptions**: Hook system handles errors
4. **Log Errors**: Use `logger.error()` with traceback
5. **Graceful Degradation**: Return original context on error

How the registry handles failures:

- **Timeout** (per hook, `asyncio.wait_for`): error log, the hook's changes are
  discarded, the chain continues. The hook task is cancelled — side effects may
  be half done.
- **Exception, invalid result, missing method**: logged, the hook counts as
  failed, the chain continues.
- If the whole chain fails, the agent loop continues with the unchanged
  messages — a broken hook only shows up in the log.

### Cache Safety

Providers cache the request prefix. **Every LLM request must be a prefix of
the next one**; a hook that breaks this pays for the whole context again on
every step.

- Nothing ticking (clock, step counter) in the system prompt or at the front of
  the history — the prompt is re-rendered every step.
- Don't rewrite earlier messages; append new content at the end.
- Find your own insert by `ChatMessage.injected_by` and replace it instead of
  adding a duplicate.
- **Every `role: user` message a hook or the loop inserts carries
  `injected_by`.** `None` means "written by a person"; context_engineer, OKF,
  tool_preload and agent_continuation rely on it to find the last human
  message. A `post_llm_call` hook that sets `continue` must set
  `continue_injected_by`.

Guards: `tests/agent/test_agent_step_budget_note.py`,
`tests/config/test_prompts_have_no_ticking_clock.py`.

### Testing

1. **Test Each Hook**: Separate test for each hook type
2. **Test Edge Cases**: Empty inputs, large inputs, invalid data
3. **Test Ordering**: Verify hooks run in correct order
4. **Test Timeouts**: Ensure hooks respect timeout limits
5. **Test Failures**: Verify error handling and isolation

### Code Quality

1. **Type Hints**: Full type annotations
2. **Docstrings**: Document purpose, args, returns
3. **Clear Names**: Descriptive hook and method names
4. **Single Responsibility**: Each hook does one thing
5. **Configuration**: Make behavior configurable

## Examples

See [Plugin Examples](../src/plugins/) for complete implementations:

- **[context_engineer](../src/plugins/context_engineer/)**: Kontext-Kompaktierung in Schichten, schont den Prompt-Cache
- **[context_summarizer](../src/plugins/context_summarizer/)**: Intelligent LLM-based summarization
- **[message_validator](../src/plugins/message_validator/)**: Message format validation
- **[request_logger](../src/plugins/request_logger/)**: Request/response logging

## Troubleshooting

### Hook Not Executing

1. Check that hook is enabled in configuration
2. Verify the hook is registered (`GET /hooks` API endpoint, or `Registered hook` in the log)
3. Check the hook type fires at all (`pre_tool_call` / `post_tool_call` never do)
4. Check `schema.yaml` has a `hooks:` key and the method name equals the hook name
5. Review log files for errors

### Circular Dependency

```
ERROR: Circular dependency in hooks for pre_llm_call: Circular dependency detected in hooks: [...]
```

Nothing is raised; the hooks of that type run in registration order.

**Solution:** Review `order` specifications in hooks, remove cycles

### Hook Timeout

```
ERROR: Hook 'my_instance.my_hook' timed out after 30.0s
```

**Solutions:**
- Increase timeout in configuration
- Optimize hook implementation
- Move expensive operations to background task

### Context Not Modified

**Issue:** Changes to context don't persist

**Solution:** Ensure you return modified context and set `modified=True`:

```python
result = HookResult(
    success=True,
    modified=True,          # Must be True
    context=modified_context  # Return modified context
)
```

### Order Not Respected

**Issue:** Hooks execute in wrong order

**Solution:**
- Use full hook names or categories in `order` — short names are silently ignored
- Check for conflicting order constraints (`Hook ordering conflict` warning)
- Inspect registered hooks via `GET /hooks`; the executed order is logged at
  debug level (`Executing N hooks for pre_llm_call: [...]`)

### Agent Override Not Working

**Issue:** Per-agent hook override has no effect

**Common Causes & Solutions:**

1. **Incorrect Hook Name Format**
   ```yaml
   # ❌ WRONG - Missing instance prefix
   hooks:
     overrides:
       inject_todo_tasks:
         enabled: true

   # ✅ CORRECT - Full <instance>.<hook> format
   hooks:
     overrides:
       todo.inject_todo_tasks:
         enabled: true
   ```

2. **Hook Not Registered**
   - The plugin's `schema.yaml` needs a `hooks:` key and the instance must be enabled in `config/plugins.yaml`
   - The `type` in `plugin.toml` is not used for hook registration

3. **Missing or Wrong PLUGIN_FACTORY**
   - Ensure `plugin.py` exports `PLUGIN_FACTORY`
   - It is called as `PLUGIN_FACTORY(name, system_config, mcp_config)`

**Debug Steps:**
```bash
# Check hook registration (shows the exact full name to use)
grep "Registered hook" logs/cli.log | grep inject_todo_tasks
```

---

**Related Documentation:**
- [Plugin Authoring Guide](./plugin_authoring.md)
- [Configuration Reference](../config/README.md)
- [API Reference](./api_reference.md)
