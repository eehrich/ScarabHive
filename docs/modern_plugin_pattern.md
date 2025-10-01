# Modern MCP Plugin Pattern

This document describes the modernized pattern for implementing MCP plugins using `SchemaBasedMCPServer` with automatic tool dispatching.

## Overview

The modern pattern eliminates boilerplate code by using a generic dispatcher that automatically routes tool calls to methods matching the tool names. No manual `call()` override is needed!

## Pattern Comparison

### ❌ Old Pattern (Before)

```python
class OldPlugin(SchemaBasedMCPServer):
    def __init__(self, name: str, config: AgentConfig, registry=None):
        super().__init__(name, config)
        self.ssl_verify = config.ssl_verify
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        # Manual routing - lots of boilerplate!
        if tool == f"{self.name}_tool1":
            return await self._handle_tool1(params)
        elif tool == f"{self.name}_tool2":
            return await self._handle_tool2(params)
        else:
            raise ValueError(f"Unknown tool: {tool}")
    
    async def _handle_tool1(self, params: dict[str, Any]) -> dict[str, Any]:
        # Implementation
        pass
```

### ✅ New Pattern (After)

```python
class ModernPlugin(SchemaBasedMCPServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
        # Extract plugin config from mcp_config
        self.some_setting = getattr(mcp_config, "some_setting", "default")
    
    # NO call() override needed! Generic dispatcher handles routing
    
    async def example_tool1(self, params: dict[str, Any]) -> dict[str, Any]:
        """Automatically called when 'example_tool1' tool is invoked.
        
        Method name MUST match the tool name in schema.yaml exactly.
        """
        # Implementation
        pass
    
    async def example_tool2(self, params: dict[str, Any]) -> dict[str, Any]:
        """Automatically called when 'example_tool2' tool is invoked."""
        # Implementation
        pass
```

## Key Changes

### 1. Constructor Signature

**Before:**
```python
def __init__(self, name: str, config: AgentConfig, registry=None)
```

**After:**
```python
def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig)
```

- `system_config`: System-wide configuration (network settings, logging, etc.)
- `mcp_config`: Plugin-specific configuration from `config.mcp_system.servers[name]`
- No `registry` parameter needed
- No `ssl_verify` attribute (removed from MCPServer)

### 2. No Manual call() Override

The generic dispatcher in `MCPServer.call()` automatically routes tool calls to methods:

```python
# When tool "example_calculator" is called:
MCPServer.call("example_calculator", params)
# ↓ Automatically routes to:
self.example_calculator(params)
```

### 3. Method Naming Convention

**Tool methods must match tool names exactly:**

- Schema defines tool: `"{{ name }}_calculator"` 
- For plugin named "example": tool name becomes `"example_calculator"`
- Method name must be: `async def example_calculator(self, params)`

### 4. No Underscore Prefix

**Before:** `async def _handle_calculator(...)`  
**After:** `async def example_calculator(...)`

Method names should NOT use underscore prefixes - they need to match the public tool names.

### 5. Configuration Access

**Before:**
```python
self.ssl_verify = config.ssl_verify  # System-wide config mixed with plugin config
self.setting = config.setting
```

**After:**
```python
# System-wide config
ssl_verify = self.system_config.network.ssl_verify

# Plugin-specific config
self.setting = getattr(self.mcp_config, "setting", "default")
```

## Benefits

1. **Less Boilerplate**: No manual routing code needed
2. **Type-Safe**: Method names match tool names - easier to verify
3. **Cleaner Separation**: System config vs plugin config clearly separated
4. **Better Errors**: Automatic error messages list available tools
5. **Modern Standards**: No legacy code, backwards compatibility, or workarounds
6. **Easier Maintenance**: Less code to maintain and test

## Example Implementation

See `src/plugins/example/server.py` for a complete working example of the modern pattern.

### Schema Definition (schema.yaml)

```yaml
tools:
  - type: function
    function:
      name: "{{ name }}_calculator"
      description: Perform arithmetic operations
      parameters:
        type: object
        properties:
          operation:
            type: string
            enum: [add, subtract, multiply, divide]
          a:
            type: number
          b:
            type: number
        required: ["operation", "a", "b"]
```

### Plugin Implementation (server.py)

```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer

class ExampleServer(SchemaBasedMCPServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
        self.precision = int(getattr(mcp_config, "precision", 2))
    
    async def example_calculator(self, params: dict[str, Any]) -> dict[str, Any]:
        """Called automatically when 'example_calculator' tool is invoked."""
        operation = params["operation"]
        a, b = params["a"], params["b"]
        
        if operation == "add":
            result = a + b
        elif operation == "subtract":
            result = a - b
        # ... etc
        
        return {"result": result, "operation": operation}
```

## Migration Guide

To migrate an existing plugin:

1. **Update imports:**
   ```python
   if TYPE_CHECKING:
       from agent_system.config.models import AgentSystemConfig, MCPConfig
   ```

2. **Update constructor:**
   - Change signature to `(name, system_config, mcp_config)`
   - Remove `registry=None` parameter
   - Update `super().__init__()` call
   - Remove `self.ssl_verify` assignment
   - Access config via `self.mcp_config` instead of `config`

3. **Remove call() override:**
   - Delete the entire `async def call(self, tool, params)` method

4. **Rename tool methods:**
   - Change `_handle_toolname` to match exact tool name
   - For plugin "example" with tool "example_calculator":
     - Rename `async def _handle_calculator(...)` 
     - To `async def example_calculator(...)`

5. **Update config access:**
   - Change `self.ssl_verify` to `self.system_config.network.ssl_verify` (if needed)
   - Change `config.setting` to `self.mcp_config.setting`

6. **Test:**
   - Verify imports work
   - Check method names match schema tool names exactly
   - Confirm no manual `call()` override exists
   - Test tool invocation works

## Testing

The modernized MCPServer includes comprehensive tests in `tests/test_mcp_mcpserver.py`:

- Constructor with modern signature
- Generic call() dispatcher  
- Automatic tool routing
- Error handling with helpful messages
- No legacy code verification

Run tests:
```bash
pytest tests/test_mcp_mcpserver.py -v
```

## Next Steps

After understanding this pattern, update remaining plugins:

1. ✅ `example` - Already modernized (reference implementation)
2. ⏳ `weather` - Update to modern pattern
3. ⏳ `twitter_search` - Update to modern pattern
4. ⏳ `yahoo_finance` - Update to modern pattern
5. ⏳ `web_scraper` - Update to modern pattern
6. ⏳ `script_interpreter` - Update to modern pattern
7. ⏳ `ibkr` - Update to modern pattern
8. ⏳ `llm_router` - Update to modern pattern
9. ⏳ `http_server` - Update to modern pattern
10. ⏳ `duckduckgo_search` - Update to modern pattern

## Common Pitfalls

❌ **Method name doesn't match tool name:**
```python
# Schema has: "example_calculator"
async def calculator(self, params):  # ❌ Wrong! Missing "example_" prefix
```

❌ **Using underscore prefix:**
```python
async def _example_calculator(self, params):  # ❌ Wrong! Has underscore prefix
```

❌ **Overriding call() unnecessarily:**
```python
async def call(self, tool, params):  # ❌ Wrong! Let MCPServer handle routing
```

✅ **Correct method naming:**
```python
# Schema has: "example_calculator"
async def example_calculator(self, params):  # ✅ Correct! Matches exactly
```

## Additional Resources

- `src/agent_system/mcp/base.py` - MCPServer base class with generic dispatcher
- `src/agent_system/mcp/schema_based.py` - SchemaBasedMCPServer implementation
- `src/plugins/example/` - Complete modernized example
- `tests/test_mcp_mcpserver.py` - Comprehensive test suite
