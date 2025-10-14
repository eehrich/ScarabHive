# Plugin Architecture: Tools vs. Hooks

## Overview

The plugin system supports **three distinct plugin types**, each serving different purposes:

### 1. **Tool-Only Plugins** (MCPServer)
Plugins that provide **MCP tools** for the agent to call.

```python
from agent_system.mcp.base import MCPServer

class MyToolPlugin(MCPServer):
    """Provides tools the agent can call."""
    
    async def list_tools(self):
        return [
            {"name": "search", "description": "Search the web"},
            {"name": "calculate", "description": "Perform calculation"}
        ]
    
    async def call_tool(self, tool_name: str, arguments: dict):
        if tool_name == "search":
            return {"results": [...]}
        elif tool_name == "calculate":
            return {"result": 42}
```

**Use cases:** Web search, data fetching, external API calls, computations

---

### 2. **Hook-Only Plugins** (PluginHook)
Plugins that **intercept agent lifecycle events** without providing tools.

```python
from agent_system.hooks import PluginHook, HookContext, HookResult

class MyHookPlugin(PluginHook):
    """Intercepts agent lifecycle without providing tools."""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # Modify messages before LLM call
        context.messages.append({"role": "system", "content": "Think step-by-step"})
        return HookResult(success=True, modified=True, context=context)
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        # Log response, extract metadata
        logger.info(f"LLM responded: {context.llm_response}")
        return HookResult(success=True, modified=False, context=context)
```

**Use cases:** Logging, context optimization, message validation, output formatting

---

### 3. **Hybrid Plugins** (MCPServer + PluginHook)
Plugins that provide **both tools AND hooks** using multiple inheritance.

```python
from agent_system.mcp.base import MCPServer
from agent_system.hooks import PluginHook, HookContext, HookResult

class MyHybridPlugin(MCPServer, PluginHook):
    """Provides both tools and lifecycle hooks."""
    
    # Tool implementation
    async def list_tools(self):
        return [{"name": "special_search", "description": "Enhanced search"}]
    
    async def call_tool(self, tool_name: str, arguments: dict):
        return {"results": [...]}
    
    # Hook implementation
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        # Log when agent calls ANY tool (including our own)
        logger.info(f"Calling tool: {context.tool_call['name']}")
        return HookResult(success=True, modified=False, context=context)
```

**Use cases:** Plugins that need both tool functionality AND lifecycle awareness

---

## Key Architectural Principles

### ✅ **Independence**
- `PluginHook` is **completely independent** of `MCPServer`
- Hook-only plugins **do NOT inherit** from `MCPServer`
- Tool-only plugins **do NOT inherit** from `PluginHook`

### ✅ **Composition over Inheritance**
- Use **multiple inheritance** only when you genuinely need BOTH capabilities
- Don't force plugins to inherit unnecessary base classes

### ✅ **Clear Separation of Concerns**
- **Tools:** What the agent can **actively call** (actions, queries)
- **Hooks:** What **passively observes/modifies** agent behavior (logging, validation, optimization)

---

## Plugin Type Selection Guide

| Scenario | Plugin Type | Base Class |
|----------|-------------|------------|
| Provide callable tools to agent | Tool-Only | `MCPServer` |
| Log/monitor agent behavior | Hook-Only | `PluginHook` |
| Optimize context before LLM calls | Hook-Only | `PluginHook` |
| Validate/sanitize messages | Hook-Only | `PluginHook` |
| Format agent output | Hook-Only | `PluginHook` |
| Provide tools + monitor their usage | Hybrid | `MCPServer, PluginHook` |
| Provide tools + optimize context | Hybrid | `MCPServer, PluginHook` |

---

## Example: Request Logger (Hook-Only)

```python
# src/plugins/request_logger/plugin.py
from agent_system.hooks import PluginHook, HookContext, HookResult

class RequestLoggerPlugin(PluginHook):
    """Logs agent requests - NO tools provided."""
    
    def __init__(self, name: str, config: dict = None):
        super().__init__(name, config or {})
        self.request_count = 0
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        self.request_count += 1
        logger.info(f"Request #{self.request_count}: {len(context.messages)} messages")
        return HookResult(success=True, modified=False, context=context)
    
    async def on_session_start(self, context: HookContext) -> HookResult:
        logger.info(f"Session started: {context.session_id}")
        return HookResult(success=True, modified=False, context=context)
```

**Note:** This plugin does NOT inherit from `MCPServer` because it provides no tools.

---

## Example: Enhanced Search (Hybrid)

```python
# src/plugins/enhanced_search/plugin.py
from agent_system.mcp.base import MCPServer
from agent_system.hooks import PluginHook, HookContext, HookResult

class EnhancedSearchPlugin(MCPServer, PluginHook):
    """Provides search tools + optimizes search queries via hooks."""
    
    # Tool functionality
    async def list_tools(self):
        return [{"name": "smart_search", "description": "AI-enhanced search"}]
    
    async def call_tool(self, tool_name: str, arguments: dict):
        query = arguments.get("query", "")
        # Perform search...
        return {"results": [...]}
    
    # Hook functionality
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        # Optimize search queries before execution
        if context.tool_call and context.tool_call.get("name") == "smart_search":
            original_query = context.tool_call["arguments"]["query"]
            optimized_query = self._optimize_query(original_query)
            context.tool_call["arguments"]["query"] = optimized_query
            return HookResult(success=True, modified=True, context=context)
        return HookResult(success=True, modified=False, context=context)
    
    def _optimize_query(self, query: str) -> str:
        # Query optimization logic...
        return query.strip().lower()
```

**Note:** This plugin uses BOTH base classes because it genuinely needs both capabilities.

---

## Configuration

### Plugin Type Declaration in `plugin.yaml`

```yaml
# Hook-only plugin
type: hooks_only
hooks:
  - name: log_pre_llm
    type: pre_llm_call
    enabled: true
    order:
      after: ["begin"]

# Tool-only plugin
type: mcp_only
# No hooks section

# Hybrid plugin
type: hybrid_with_hooks
hooks:
  - name: optimize_query
    type: pre_tool_call
    enabled: true
```

### Global Hook Configuration in `config/plugins.yaml`

```yaml
hooks:
  enabled: true
  default_timeout: 30.0
  overrides:
    request_logger.log_pre_llm:
      enabled: true
      timeout: 5.0
      order:
        after: ["begin"]
        before: ["context_optimizer"]
```

---

## Migration Notes

### ❌ **Old Pattern (Incorrect)**
```python
class HookOnlyPlugin(MCPServer, PluginHook):  # ❌ Unnecessary MCPServer inheritance
    async def list_tools(self):
        return []  # Empty because we don't provide tools
    
    async def on_pre_llm_call(self, context):
        # Hook logic...
```

### ✅ **New Pattern (Correct)**
```python
class HookOnlyPlugin(PluginHook):  # ✅ Only inherit what you need
    async def on_pre_llm_call(self, context):
        # Hook logic...
```

---

## Summary

- **Tool-Only:** `MCPServer` → Provides callable tools
- **Hook-Only:** `PluginHook` → Observes/modifies lifecycle
- **Hybrid:** `MCPServer, PluginHook` → Provides tools AND hooks

**Choose the simplest base class that meets your needs.**
