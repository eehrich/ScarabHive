# SchemaBasedPluginWebInterface

## Overview

`SchemaBasedPluginWebInterface` is a base class for plugins with web interfaces that load their configuration from `schema.yaml` files, eliminating code duplication across web-only and hook+web plugins.

## Design Pattern

Follows the same pattern as other schema-based components:
- `SchemaBasedMCPServer` - For MCP servers
- `SchemaBasedAgent` - For agents
- `SchemaBasedPluginHook` - For hook plugins
- **`SchemaBasedPluginWebInterface`** - For web-only and hook+web plugins

## Architecture

```
SchemaBasedMixin (schema_mixin.py)
    │
    ├─> SchemaBasedMCPServer (mcp/schema_based.py)
    ├─> SchemaBasedAgent (agents/schema_based.py)
    ├─> SchemaBasedPluginHook (hooks/schema_based.py)
    └─> SchemaBasedPluginWebInterface (plugins/web_base.py) ← NEW
```

## Usage

### Web-Only Plugins

For plugins that **only** provide web UI (no MCP tools, no hooks):

```python
from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

class MyWebPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, mcp_config)
        
        # Plugin-specific initialization
        self.web_endpoints = MyWebEndpoints(...)
    
    # get_schema_data() inherited - no need to implement!
    
    def get_tools(self):
        """No MCP tools - web UI only"""
        return []
    
    def get_web_router(self):
        """Return FastAPI router"""
        return self.web_endpoints.get_web_router()
```

### Hook+Web Plugins

For plugins that provide **both** hooks and web UI:

```python
from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
from agent_system.hooks import SchemaBasedPluginHook

class MyHooks(SchemaBasedPluginHook):
    async def my_hook(self, context: HookContext) -> HookResult:
        # Hook implementation
        pass

class MyHybridPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, mcp_config)
        
        plugin_dir = Path(__file__).parent
        
        # Create hooks plugin
        self.hooks_plugin = MyHooks(plugin_dir)
        
        # Create web factory
        self.web_factory = MyWebFactory()
    
    # Hook interface
    def get_hooks(self):
        return self.hooks_plugin.get_hooks()
    
    async def execute_hook(self, hook_type, context):
        return await self.hooks_plugin.execute_hook(hook_type, context)
    
    # get_schema_data() inherited - no need to implement!
    
    # Web interface
    def get_web_router(self):
        return self.web_factory.get_web_router()
```

## Benefits

### Before (Duplicate Code)

Every web-only and hook+web plugin had to implement identical `get_schema_data()` methods:

```python
def get_schema_data(self):
    """Return schema for web UI configuration"""
    from agent_system.plugins.schema_loader import load_schema_from_dir
    from pathlib import Path
    plugin_dir = Path(__file__).parent
    return load_schema_from_dir(plugin_dir, {'name': self.name})
```

**Result**: Code duplication across 4+ plugins (user_management, message_debugger, context_summarizer, context_usage_tracker)

### After (Inheritance)

Plugins inherit `get_schema_data()` from base class:

```python
class MyPlugin(SchemaBasedPluginWebInterface):
    # get_schema_data() inherited - no need to implement!
    pass
```

**Result**: Single implementation in base class, consistent behavior across all plugins

## Implementation Details

### Base Class (`SchemaBasedPluginWebInterface`)

Located in: `src/agent_system/plugins/web_base.py`

```python
class SchemaBasedPluginWebInterface(SchemaBasedMixin):
    """Base class for plugins with web interfaces."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Initialize schema mixin
        self._init_schema_mixin()
    
    def get_schema_data(self) -> dict[str, Any]:
        """Get the full loaded schema data."""
        return self._load_schema()
```

### Schema Loading

Uses `SchemaBasedMixin._load_schema()` which:
1. Determines plugin directory automatically
2. Loads `schema.yaml` from plugin directory
3. Renders Jinja2 templates with `get_template_vars()`
4. Caches schema data for performance
5. Validates schema structure

### Plugin Registry Integration

The plugin registry calls `get_schema_data()` when registering plugins:

```python
# In PluginMCPRegistry.register_existing_plugin_instance()
if hasattr(plugin_instance, 'get_schema_data'):
    schema = plugin_instance.get_schema_data()
    # Use schema for web UI configuration
```

## Plugin Types Comparison

| Plugin Type | MCP Tools | Hooks | Web UI | Base Class |
|-------------|-----------|-------|--------|------------|
| **MCP+Web** | ✅ Delegate to mcp_server | ❌ | ✅ | Hybrid (custom) |
| **Web-Only** | ❌ | ❌ | ✅ | SchemaBasedPluginWebInterface |
| **Hook+Web** | ❌ | ✅ | ✅ | SchemaBasedPluginWebInterface |

### MCP+Web Plugins (ssh_control, log_viewer)

**Pattern**: Delegate `get_schema_data()` to MCP server component

```python
class SSHControlHybridPlugin:
    def __init__(self, name, system_config, mcp_config):
        self.mcp_server = SSHControlServer(...)  # SchemaBasedMCPServer
    
    def get_schema_data(self):
        """Delegate to MCP server"""
        return self.mcp_server.get_schema_data()
```

**Reason**: MCP server component already has schema loaded via `SchemaBasedMCPServer`, so just delegate to it.

### Web-Only Plugins (user_management)

**Pattern**: Inherit from `SchemaBasedPluginWebInterface`

```python
class UserManagementPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name, system_config, mcp_config):
        super().__init__(name, system_config, mcp_config)
    
    # get_schema_data() inherited
```

**Reason**: No MCP server component, need schema for web_ui configuration only.

### Hook+Web Plugins (message_debugger, context_summarizer, context_usage_tracker)

**Pattern**: Inherit from `SchemaBasedPluginWebInterface`

```python
class MessageDebuggerHybridPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name, system_config, mcp_config):
        super().__init__(name, system_config, mcp_config)
        self.hooks_plugin = MessageDebuggerHooks(...)
    
    # get_schema_data() inherited
```

**Reason**: Hooks component uses separate `SchemaBasedPluginHook`, need schema for web_ui configuration.

## Schema Format

Plugins must have a `schema.yaml` file in their directory with `web_ui` section:

```yaml
name: my_plugin
description: My plugin description

web_ui:
  button:
    text: "My Plugin"
    icon: "🔧"
  
  panel:
    title: "My Plugin Panel"
    endpoint: "/plugins/my_plugin/panel"
    type: "iframe"
    width: "800px"
    height: "600px"

tools:
  # MCP tools (optional)
  
hooks:
  # Hook definitions (optional)
  
config:
  # Plugin configuration (optional)
```

## Migration Guide

### Migrating Existing Plugins

**Before**:
```python
class MyPlugin:
    def __init__(self, name, system_config, mcp_config):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
    
    def get_schema_data(self):
        from agent_system.plugins.schema_loader import load_schema_from_dir
        from pathlib import Path
        plugin_dir = Path(__file__).parent
        return load_schema_from_dir(plugin_dir, {'name': self.name})
```

**After**:
```python
from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

class MyPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name, system_config, mcp_config):
        super().__init__(name, system_config, mcp_config)
    
    # get_schema_data() inherited - remove manual implementation!
```

### Steps

1. Import `SchemaBasedPluginWebInterface`:
   ```python
   from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
   ```

2. Inherit from base class:
   ```python
   class MyPlugin(SchemaBasedPluginWebInterface):
   ```

3. Call `super().__init__()` in constructor:
   ```python
   def __init__(self, name, system_config, mcp_config):
       super().__init__(name, system_config, mcp_config)
   ```

4. Remove manual `get_schema_data()` implementation:
   ```python
   # DELETE THIS:
   def get_schema_data(self):
       from agent_system.plugins.schema_loader import load_schema_from_dir
       from pathlib import Path
       plugin_dir = Path(__file__).parent
       return load_schema_from_dir(plugin_dir, {'name': self.name})
   ```

5. Add comment indicating inherited method:
   ```python
   # get_schema_data() inherited from SchemaBasedPluginWebInterface
   ```

## Testing

All plugins using `SchemaBasedPluginWebInterface` should:

1. **Appear in `/api/plugins/ui`** endpoint
2. **Have correct `web_ui` configuration** from schema.yaml
3. **Load panels correctly** when button clicked
4. **Pass existing tests** without modifications

Example test:
```python
async def test_plugin_appears_in_ui():
    response = await client.get("/api/plugins/ui")
    plugins = response.json()
    
    # Find our plugin
    plugin = next(p for p in plugins if p["id"] == "my_plugin")
    
    assert plugin["enabled"] is True
    assert plugin["button_text"] == "My Plugin"
    assert plugin["panel_endpoint"] == "/plugins/my_plugin/panel"
```

## Related Components

- **SchemaBasedMixin** (`agent_system/mcp/schema_mixin.py`) - Core schema loading logic
- **SchemaBasedMCPServer** (`agent_system/mcp/schema_based.py`) - For MCP servers
- **SchemaBasedPluginHook** (`agent_system/hooks/schema_based.py`) - For hook plugins
- **PluginMCPRegistry** (`agent_system/plugins/mcp_adapter.py`) - Uses `get_schema_data()`
- **load_schema_from_dir** (`agent_system/plugins/schema_loader.py`) - Schema loading utility

## See Also

- [Plugin Architecture](plugin_architecture.md)
- [Schema-Based Components](schema_based_components.md)
- [Plugin Authoring Guide](plugin_authoring.md)
