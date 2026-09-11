# Plugin Hook System

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
8. [Examples](#examples)
9. [Troubleshooting](#troubleshooting)

## Overview

The Plugin Hook System provides lifecycle interception points for extending agent behavior without modifying core code. Hooks allow plugins to observe and modify agent operations at key points in the execution flow.

### Key Features

- **7 Lifecycle Points**: Pre/post LLM, pre/post tool, format output, session start/end
- **Flexible Ordering**: Named dependencies with topological sorting
- **Schema-Based Pattern**: Declarative hook definitions in YAML
- **Error Isolation**: Hook failures don't crash the agent
- **Global Configuration**: Override hook behavior per agent/environment
- **Metadata Tracking**: Detailed execution statistics and audit trails

### Architecture

```
Agent Execution Flow
  ↓
  SESSION_START hooks
  ↓
  ┌─ PRE_LLM_CALL hooks
  │    ↓
  │  LLM Execution
  │    ↓
  └─ POST_LLM_CALL hooks
  ↓
  ┌─ PRE_TOOL_CALL hooks
  │    ↓
  │  Tool Execution
  │    ↓
  └─ POST_TOOL_CALL hooks
  ↓
  FORMAT_OUTPUT hooks
  ↓
  SESSION_END hooks
```

### Use Cases

- **Context Management**: Optimize message history before LLM calls
- **Validation**: Validate message format and content
- **Logging**: Audit agent behavior and track metrics
- **Transformation**: Modify inputs/outputs dynamically
- **Security**: Sanitize content, enforce policies
- **Monitoring**: Track performance, costs, usage patterns

## Hook Types

### PRE_LLM_CALL

**Trigger:** Before sending messages to LLM
**Use Cases:** Context optimization, prompt injection, validation
**Can Modify:** Messages, agent configuration

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
- `context_optimizer`: Removes duplicates, truncates long messages
- `context_summarizer`: Intelligently summarizes older messages
- `message_validator`: Validates message format

### POST_LLM_CALL

**Trigger:** After receiving LLM response
**Use Cases:** Response validation, logging, statistics
**Can Modify:** LLM response, metadata

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
- `context_optimizer`: Logs context statistics

### PRE_TOOL_CALL

> ⚠️ **Not wired yet:** The hook type, registry routing and
> `HookIntegrationManager.execute_pre_tool_hooks` all exist, but the agent's
> tool-execution loop does not call them — hooks of this type currently
> **never fire**. Same for POST_TOOL_CALL. Kept deliberately as a planned
> extension point (see `docs/agent_package_architecture_review.md`, Befund E);
> do not build plugins on it until it is wired into `ToolExecutionManager`.

**Trigger:** Before executing a tool
**Use Cases:** Parameter validation, access control, logging
**Can Modify:** Tool parameters, execution decision

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

**Trigger:** After tool execution
**Use Cases:** Result validation, error handling, logging
**Can Modify:** Tool result, error handling

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

**Trigger:** Before returning output to user
**Use Cases:** Multi-format rendering (HTML, ANSI, text), filtering
**Can Modify:** Final output content based on target format

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

**Trigger:** When agent session begins
**Use Cases:** Initialization, logging, setup
**Can Modify:** Session metadata, initialization

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

**Trigger:** When agent session ends
**Use Cases:** Cleanup, statistics, logging
**Can Modify:** Cleanup operations, final statistics

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
    hook_type: HookType              # Type of hook being executed
    request_id: str                  # Unique request ID for tracking
    session_id: Optional[str]        # Session identifier
    agent: Optional[Any]             # Reference to agent instance
    agent_name: Optional[str]        # Name of the agent
    messages: Optional[List[Dict]]   # Conversation messages
    llm_response: Optional[Any]      # LLM response (post-LLM only)
    tool_call: Optional[Dict]        # Tool call info (tool hooks only)
    tool_result: Optional[Any]       # Tool result (post-tool only)
    output: Optional[str]            # Output text (format hook only)
    metadata: Optional[Dict]         # Additional context metadata
    step: Optional[int]              # Execution step number
    llm: Optional[Any]               # LLM instance for hook use
```

### HookResult

Return value from hook execution:

```python
@dataclass
class HookResult:
    success: bool                    # Hook executed successfully
    modified: bool                   # Context was modified
    context: HookContext             # Modified (or original) context
    metadata: Optional[Dict] = None  # Additional result metadata
    error: Optional[str] = None      # Error message if failed
```

**Important:**
- Set `modified=True` if you changed the context
- Return modified context in `context` field
- Use `metadata` for statistics, audit info
- Set `success=False` and provide `error` on failure

## Hook Ordering

### Named Dependencies

Hooks use unique names and specify dependencies:

```yaml
hooks:
  - name: my_hook
    type: pre_llm_call
    order:
      after: ["begin", "optimize_context"]  # Run after these hooks
      before: ["validate_messages", "end"]  # Run before these hooks
```

### Special Names

- `begin`: Virtual hook at the start of each type
- `end`: Virtual hook at the end of each type

### Topological Sort

The `HookRegistry` uses topological sort (Kahn's algorithm) to determine execution order:

1. Parse all hooks and their dependencies
2. Build dependency graph
3. Detect circular dependencies (raises error)
4. Sort hooks topologically
5. Execute in sorted order

### Example Ordering

```yaml
# config/plugins.yaml
hooks:
  enabled: true
  overrides:
    context_optimizer.optimize_context:
      order:
        after: ["begin"]
        before: ["summarize_context"]

    context_summarizer.summarize_context:
      order:
        after: ["optimize_context"]
        before: ["validate_messages"]

    message_validator.validate_messages:
      order:
        after: ["summarize_context"]
        before: ["end"]
```

**Execution Order:**
1. `begin` (virtual)
2. `optimize_context`
3. `summarize_context`
4. `validate_messages`
5. `end` (virtual)

## Schema-Based Hooks

Recommended pattern for new hook plugins.

### Directory Structure

```
src/plugins/my_plugin/
├── plugin.py        # Factory function
├── hooks.py         # Hook implementation
├── schema.yaml      # Hook definitions + config
└── README.md        # Documentation
```

### schema.yaml

```yaml
# Hook definitions
hooks:
  - name: my_hook_handler        # Must match method name exactly
    type: pre_llm_call
    enabled: true
    timeout: 30.0
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


### Instanz-Default per `hook_config` (seit 2026-09-02)

Das Schema spricht für den Plugin-TYP. Läuft dasselbe Plugin mehrfach als
Instanz (z. B. `context_summarizer` und `writer_context_summarizer`), setzt
die Server-Config einer Instanz ihren eigenen Registrier-Default:

```yaml
# config/plugins.yaml bzw. eingebundene Plugin-Configs
servers:
  writer_context_summarizer:
    type: context_summarizer
    hook_config:
      enabled: false   # diese Instanz startet AUS; Agenten schalten sie
                       # per hooks.overrides (exakter instanz.hook-Key) an
```

Wirkt **nur absenkend**: `enabled: false` schaltet die Hooks dieser Instanz
aus; `enabled: true` hebt einen Schema-Default NICHT an (einen Default
anzuheben ist Operator-Sache — globale `hooks.overrides`). Vorrang bei der
Registrierung: Schema-Eintrag < Instanz-Absenkung < globale `hooks.overrides`
< globaler Master-Schalter `hooks.enabled: false`. Zur Laufzeit gewinnt
darüber der Agent-Override (exakter Key), siehe oben.

### hooks.py

```python
from pathlib import Path
from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

class MyPlugin(SchemaBasedPluginHook):
    def __init__(self, plugin_dir: Path | str):
        super().__init__(plugin_dir)

        # Load config from schema
        config = self.get_config()
        self.max_items = config.get('max_items', {}).get('default', 100)

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

def PLUGIN_FACTORY() -> MyPlugin:
    plugin_dir = Path(__file__).parent
    return MyPlugin(plugin_dir)
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

**CRITICAL:** Hooks are registered and referenced using the **full name** format: `plugin_name.hook_name`

**Examples:**
- `todo_management.inject_todo_tasks`
- `markdown_formatter.format_markdown_output`
- `context_optimizer.optimize_context`

**Why This Matters:**
- Enables multiple plugins to have hooks with the same base name
- Required for per-agent hook overrides to work correctly
- Ensures proper hook filtering and execution control

**Convention Breakdown:**
```yaml
# In plugin schema.yaml (just the hook name)
hooks:
  - name: inject_todo_tasks
    type: pre_llm_call
    enabled: false

# In registry (full name with plugin prefix)
# Registered as: todo_management.inject_todo_tasks

# In agent config overrides (full name required)
agent_config:
  hooks:
    overrides:
      todo_management.inject_todo_tasks:
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
  enabled: true
  default_timeout: 30.0
  overrides:
    plugin_name.hook_name:
      enabled: false              # Disable specific hook
      timeout: 60.0               # Override timeout
      order:
        after: ["other_hook"]     # Override order
```

Ein globaler Override kennt genau diese drei Keys (`enabled`, `timeout`,
`order`) — der Loader liest nichts anderes. Plugin-spezifische Parameter
gehören in die Hook-Metadaten des Plugins bzw. die Agent-Overrides
(`agent_config.hooks.overrides`), nicht hierher.

### Agent-Level Config

Per-agent hook configuration with overrides:

```yaml
# config/plugins.yaml
agents:
  meta_agent:
    agent_config:
      hooks:
        enabled: true
        overrides:
          # Enable globally disabled hook for this agent
          todo_management.inject_todo_tasks:
            enabled: true
            max_tasks: 20
            filter_status: ["not-started", "in-progress", "blocked"]

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
1. **Hook has agent override** → Use override value (ignores global state)
2. **No agent override** → Use global `enabled` state from schema.yaml
3. **Hook globally disabled + agent enables** → Hook executes for this agent only
4. **Hook globally enabled + agent disables** → Hook skipped for this agent only

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
      todo_management.inject_todo_tasks:
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

- **[context_optimizer](../src/plugins/context_optimizer/)**: Basic context optimization
- **[context_summarizer](../src/plugins/context_summarizer/)**: Intelligent LLM-based summarization
- **[message_validator](../src/plugins/message_validator/)**: Message format validation
- **[request_logger](../src/plugins/request_logger/)**: Request/response logging

## Troubleshooting

### Hook Not Executing

1. Check that hook is enabled in configuration
2. Verify plugin is loaded (`GET /hooks` API endpoint)
3. Check hook type matches execution point
4. Review log files for errors

### Circular Dependency Error

```
CircularDependencyError: Circular dependency detected: A -> B -> C -> A
```

**Solution:** Review `order` specifications in hooks, remove cycles

### Hook Timeout

```
WARNING: Hook 'my_hook' exceeded timeout (30.0s)
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
- Verify `order` specifications are correct
- Check for conflicting order constraints
- Use CLI to inspect actual order: `backlog hooks list --type pre_llm_call`

### Agent Override Not Working

**Issue:** Per-agent hook override has no effect

**Common Causes & Solutions:**

1. **Incorrect Hook Name Format**
   ```yaml
   # ❌ WRONG - Missing plugin prefix
   hooks:
     overrides:
       inject_todo_tasks:
         enabled: true

   # ✅ CORRECT - Full plugin.hook_name format
   hooks:
     overrides:
       todo_management.inject_todo_tasks:
         enabled: true
   ```

2. **Plugin Type Not Hybrid**
   - Plugin must have hooks in its `type` list (e.g., `type: [hooks]` or `type: [mcp-server, hooks]`)
   - Check `src/plugins/{plugin}/plugin.yaml`

3. **Missing PLUGIN_FACTORY**
   - Ensure `plugin.py` exports `PLUGIN_FACTORY` function
   - Standard pattern: `def PLUGIN_FACTORY(...) -> ServerClass`

4. **Hook Filter Signature**
   - Filter must accept `(hook_name: str, default_enabled: bool)` parameters
   - Registry passes global enabled state to allow proper override logic

**Debug Steps:**
```bash
# 1. Check hook registration (should show full name)
grep "Registered hook" logs/cli.log | grep todo_management

# 2. Check agent config loading
grep "HookIntegrationManager for meta_agent" logs/cli.log

# 3. Check filter execution
grep "is_hook_enabled called.*todo_management" logs/cli.log

# 4. Verify hook execution
grep "TodoHook\|inject_todo" logs/cli.log
```

---

**Related Documentation:**
- [Plugin Authoring Guide](./plugin_authoring.md)
- [Configuration Reference](../config/README.md)
- [API Reference](./api_reference.md)
