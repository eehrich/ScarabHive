"""
Schema-based hook plugin base class.

Provides a base class for hook plugins that define their hooks declaratively
in a schema.yaml file, analogous to SchemaBasedMCPServer for MCP tools.

Example schema.yaml:
```yaml
hooks:
  - name: optimize_context
    type: PRE_LLM_CALL
    description: Optimize context before LLM call
    enabled: true
    priority: 10
    handler: optimize_context  # Method name on plugin class
    
  - name: log_stats
    type: POST_LLM_CALL
    description: Log context statistics
    enabled: true
    priority: 5
    handler: log_context_stats

config:
  max_total_tokens: 100000
  preserve_system_messages: true
```

Usage:
```python
class MyPlugin(SchemaBasedPluginHook):
    async def optimize_context(self, context: HookContext) -> HookResult:
        # Handler implementation
        return HookResult(...)
    
    async def log_context_stats(self, context: HookContext) -> HookResult:
        # Handler implementation
        return HookResult(...)
```
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .plugin_hook import PluginHook, HookContext, HookResult

logger = logging.getLogger(__name__)


class SchemaBasedPluginHook(PluginHook):
    """Base class for schema-driven hook plugins.
    
    Automatically loads hook definitions and configuration from schema.yaml
    and dispatches to handler methods by name.
    
    Subclasses should:
    1. Create a schema.yaml file in the same directory
    2. Implement handler methods referenced in schema.yaml
    3. Handler methods should match signature: async def handler(context: HookContext) -> HookResult
    """
    
    def __init__(self, plugin_dir: Path | str):
        """Initialize schema-based plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
        """
        super().__init__()
        self.plugin_dir = Path(plugin_dir)
        self._schema = self._load_schema()
        self._config = self._schema.get("config", {})
        self._hooks = self._schema.get("hooks", [])
    
    def _load_schema(self) -> dict[str, Any]:
        """Load schema.yaml from plugin directory.
        
        Returns:
            Schema dictionary with hooks and config sections
            
        Raises:
            FileNotFoundError: If schema.yaml doesn't exist
            yaml.YAMLError: If schema.yaml is invalid
        """
        schema_path = self.plugin_dir / "schema.yaml"
        if not schema_path.exists():
            raise FileNotFoundError(
                f"schema.yaml not found in {self.plugin_dir}. "
                f"SchemaBasedPluginHook requires a schema.yaml file."
            )
        
        with open(schema_path, encoding="utf-8") as f:
            schema = yaml.safe_load(f)
        
        if not isinstance(schema, dict):
            raise ValueError(f"schema.yaml must contain a dict, got {type(schema)}")
        
        return schema
    
    def get_hooks(self) -> list[dict[str, Any]]:
        """Get hook definitions from schema.
        
        Returns:
            List of hook definitions with name, type, handler, etc.
        """
        return self._hooks
    
    def get_config(self) -> dict[str, Any]:
        """Get configuration from schema.
        
        Returns:
            Configuration dictionary from schema.yaml config section
        """
        return self._config
    
    async def _dispatch_hook(self, hook_name: str, context: HookContext) -> HookResult:
        """Dispatch hook execution to handler method.
        
        Args:
            hook_name: Name of the hook to execute
            context: Hook execution context
            
        Returns:
            Result from handler method
            
        Raises:
            AttributeError: If handler method doesn't exist
        """
        # Find hook definition in schema
        hook_def = None
        for h in self._hooks:
            if h.get("name") == hook_name:
                hook_def = h
                break
        
        if not hook_def:
            logger.warning(f"Hook '{hook_name}' not found in schema for {self.__class__.__name__}")
            return HookResult(modified_context=context)
        
        # Get handler method name from schema
        handler_name = hook_def.get("handler")
        if not handler_name:
            logger.warning(f"No handler specified for hook '{hook_name}' in {self.__class__.__name__}")
            return HookResult(modified_context=context)
        
        # Call handler method
        handler = getattr(self, handler_name, None)
        if not handler:
            raise AttributeError(
                f"Handler method '{handler_name}' not found on {self.__class__.__name__}. "
                f"Hook '{hook_name}' references non-existent handler."
            )
        
        if not callable(handler):
            raise TypeError(
                f"Handler '{handler_name}' on {self.__class__.__name__} is not callable"
            )
        
        # Execute handler
        return await handler(context)
    
    # Lifecycle hook methods that dispatch to schema-defined handlers
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Pre-LLM hook - dispatches to handlers for PRE_LLM_CALL hooks."""
        # Find all PRE_LLM_CALL hooks and execute their handlers
        results = []
        for hook in self._hooks:
            if hook.get("type") == "PRE_LLM_CALL" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                # Chain context modifications
                if result.modified_context:
                    context = result.modified_context
        
        # Return last result (with accumulated modifications)
        return results[-1] if results else HookResult(modified_context=context)
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        """Post-LLM hook - dispatches to handlers for POST_LLM_CALL hooks."""
        results = []
        for hook in self._hooks:
            if hook.get("type") == "POST_LLM_CALL" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified_context:
                    context = result.modified_context
        
        return results[-1] if results else HookResult(modified_context=context)
    
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        """Pre-tool hook - dispatches to handlers for PRE_TOOL_CALL hooks."""
        results = []
        for hook in self._hooks:
            if hook.get("type") == "PRE_TOOL_CALL" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified_context:
                    context = result.modified_context
        
        return results[-1] if results else HookResult(modified_context=context)
    
    async def on_post_tool_call(self, context: HookContext) -> HookResult:
        """Post-tool hook - dispatches to handlers for POST_TOOL_CALL hooks."""
        results = []
        for hook in self._hooks:
            if hook.get("type") == "POST_TOOL_CALL" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified_context:
                    context = result.modified_context
        
        return results[-1] if results else HookResult(modified_context=context)
    
    async def on_format_output(self, context: HookContext) -> HookResult:
        """Format output hook - dispatches to handlers for FORMAT_OUTPUT hooks."""
        results = []
        for hook in self._hooks:
            if hook.get("type") == "FORMAT_OUTPUT" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified_context:
                    context = result.modified_context
        
        return results[-1] if results else HookResult(modified_context=context)
    
    async def on_session_start(self, context: HookContext) -> HookResult:
        """Session start hook - dispatches to handlers for SESSION_START hooks."""
        results = []
        for hook in self._hooks:
            if hook.get("type") == "SESSION_START" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified_context:
                    context = result.modified_context
        
        return results[-1] if results else HookResult(modified_context=context)
    
    async def on_session_end(self, context: HookContext) -> HookResult:
        """Session end hook - dispatches to handlers for SESSION_END hooks."""
        results = []
        for hook in self._hooks:
            if hook.get("type") == "SESSION_END" and hook.get("enabled", True):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified_context:
                    context = result.modified_context
        
        return results[-1] if results else HookResult(modified_context=context)
