"""Schema-based Mixin - Common functionality for schema-based MCP servers and agents.

This mixin provides shared functionality for loading and managing schema.yaml files,
eliminating code duplication between SchemaBasedMCPServer and SchemaBasedAgent.

Features:
- Automatic schema.yaml loading with template variable support
- Generic tool dispatcher (routes calls to methods automatically)
- Robust plugin directory resolution
- Full schema caching and access
- Development utilities (cache clearing)
"""
from __future__ import annotations

import asyncio
import logging
import importlib.util
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SchemaBasedMixin:
    """Mixin providing schema-based tool loading and dispatching.
    
    This mixin can be used by any MCPServer subclass to add automatic
    schema.yaml loading and generic tool dispatching.
    
    Classes using this mixin should:
    1. Call super().__init__() to initialize caches
    2. Optionally override get_template_vars() for custom template variables
    3. Optionally override _get_method_name() for custom tool → method routing
    4. Implement methods matching their tool names
    
    Example:
        class MyServer(MCPServer, SchemaBasedMixin):
            def __init__(self, ...):
                super().__init__(...)
                self._init_schema_mixin()
            
            async def my_tool(self, params: dict) -> Any:
                # Tool implementation
                pass
    """
    
    # Type hints for attributes that will be provided by MCPServer
    name: str
    
    def _init_schema_mixin(self) -> None:
        """Initialize schema mixin caches.
        
        Must be called in __init__ of classes using this mixin.
        """
        self._tools_cache: list[dict[str, Any]] | None = None
        self._schema_cache: dict[str, Any] | None = None
    
    def _get_plugin_directory(self) -> Path:
        """Get the directory containing the plugin module.
        
        This method determines the plugin directory by inspecting the module
        path of the plugin class. It works for both filesystem and packaged plugins.
        """
        # Get the module where this plugin class is defined
        module = self.__class__.__module__
        
        try:
            # Try to find the module spec
            spec = importlib.util.find_spec(module)
            if spec and spec.origin:
                # Get the directory containing the module file
                module_file = Path(spec.origin)
                return module_file.parent
        except Exception:
            logger.warning(f"Could not determine plugin directory for {module}")
        
        # Fallback: try to construct path from module name
        # For plugins in src/plugins/plugin_name/server.py pattern
        if "plugins." in module:
            module_parts = module.split(".")
            if len(module_parts) >= 2:
                # Find 'plugins' in the path and construct directory
                try:
                    plugins_index = module_parts.index("plugins")
                    if plugins_index + 1 < len(module_parts):
                        plugin_name = module_parts[plugins_index + 1]
                        # Try common plugin directory patterns
                        possible_paths = [
                            Path("src") / "plugins" / plugin_name,
                            Path("plugins") / plugin_name,
                            Path(".") / "plugins" / plugin_name,
                        ]
                        for path in possible_paths:
                            if path.exists() and (path / "schema.yaml").exists():
                                return path
                except ValueError:
                    pass
        
        # Last resort: raise an error with helpful message
        raise RuntimeError(
            f"Cannot determine plugin directory for {self.__class__.__name__}. "
            f"Module: {module}. Please ensure the plugin follows standard directory structure."
        )
    
    def get_template_vars(self) -> dict[str, Any]:
        """Get template variables for schema rendering.
        
        Override this method in subclasses to provide custom template variables
        for schema.yaml rendering. The base implementation provides the plugin name.
        
        Returns:
            Dictionary of template variables to pass to Jinja2 rendering.
        """
        return {"name": self.name}
    
    def _load_schema(self) -> dict[str, Any]:
        """Load and cache the plugin's schema.yaml file.
        
        Returns:
            The parsed schema data as a dictionary.
            
        Raises:
            RuntimeError: If schema.yaml is missing or invalid.
        """
        if self._schema_cache is not None:
            return self._schema_cache
        
        try:
            from agent_system.plugins.schema_loader import load_schema_from_dir
            
            plugin_dir = self._get_plugin_directory()
            template_vars = self.get_template_vars()
            
            schema_data = load_schema_from_dir(
                plugin_dir,
                template_vars=template_vars
            )
            
            if not schema_data:
                raise RuntimeError(
                    f"Missing or invalid schema.yaml for {self.name} plugin in {plugin_dir}"
                )
            
            self._schema_cache = schema_data
            logger.debug(
                f"Loaded schema for {self.name} plugin from {plugin_dir} "
                f"with template_vars={template_vars}"
            )
            return schema_data
            
        except Exception as e:
            raise RuntimeError(
                f"Failed to load schema for {self.name} plugin: {e}"
            ) from e
    
    def get_tools(self) -> list[dict[str, Any]]:
        """Load tools from the plugin's schema.yaml file.
        
        This method automatically loads and parses the schema.yaml file
        from the plugin's directory.
        
        Returns:
            List of tool definitions in OpenAI function format.
            
        Raises:
            RuntimeError: If schema is missing, invalid, or doesn't use multi-tool format.
        """
        if self._tools_cache is not None:
            return self._tools_cache
        
        schema_data = self._load_schema()
        
        # Only support multi-tool format
        if 'tools' not in schema_data:
            raise RuntimeError(
                f"{self.name} plugin schema must contain 'tools' array. "
                f"Found keys: {list(schema_data.keys())}"
            )
        
        tools = schema_data['tools']
        if not isinstance(tools, list):
            raise RuntimeError(
                f"{self.name} plugin schema 'tools' must be a list"
            )
        
        self._tools_cache = tools
        return tools
    
    def get_schema_data(self) -> dict[str, Any]:
        """Get the full loaded schema data.
        
        This method provides access to the complete schema data, not just the tools.
        Useful for plugins that need additional schema information.
        
        Returns:
            The complete parsed schema data.
        """
        return self._load_schema()
    
    def clear_schema_cache(self) -> None:
        """Clear the cached schema and tools data.
        
        This forces the schema to be reloaded on the next access.
        Useful for development and testing.
        """
        self._schema_cache = None
        self._tools_cache = None
        logger.debug(f"Cleared schema cache for {self.name} plugin")
    
    def _get_method_name(self, tool_name: str) -> str:
        """Convert tool name to method name with optional prefix stripping.
        
        This method checks if the tool name starts with "{name}_" prefix
        and strips it if present. This works for both MCP servers and agents.
        
        Examples:
        - Tool: "basic_agent_execute_task" with name="basic_agent" 
          → Method: "execute_task"
        - Tool: "my_server_search" with name="my_server" 
          → Method: "search"
        - Tool: "search" (no prefix) 
          → Method: "search"
        
        Override this method in subclasses for more complex routing logic.
        
        Args:
            tool_name: The tool name from the MCP call
            
        Returns:
            The method name to call on self
        """
        # Strip the "{name}_" prefix if present
        prefix = f"{self.name}_"
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
        # Return as-is if no prefix found
        return tool_name
    
    def _get_available_tool_names(self) -> list[str]:
        """Helper to get list of available tool names for error messages."""
        try:
            tools = self.get_tools()
            tool_names = []
            for t in tools:
                if isinstance(t, dict):
                    # Support both formats: {"function": {...}} and {"name": ...}
                    if 'function' in t:
                        name = t['function'].get('name')
                    else:
                        name = t.get('name')
                    if name:
                        tool_names.append(name)
            return tool_names
        except Exception:
            return []
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Generic tool dispatcher that routes to tool methods by name.
        
        Automatically calls the method matching the tool name.
        Derived classes just need to implement methods matching their tool names.
        
        The method name is determined by _get_method_name(), which can be
        overridden for custom routing (e.g., stripping prefixes).
        
        Example:
            If get_tools() returns a tool named "search_tweets",
            this will call self.search_tweets(params)
        
        Args:
            tool: The tool name to execute
            params: Parameters to pass to the tool method
            
        Returns:
            The result from the tool method
            
        Raises:
            ValueError: If tool is not found or not callable
        """
        # Convert tool name to method name
        method_name = self._get_method_name(tool)
        
        # Check if the tool method exists
        if not hasattr(self, method_name):
            available = self._get_available_tool_names()
            raise ValueError(
                f"Tool '{tool}' not found in {self.name}. "
                f"Available tools: {available}. "
                f"Expected method: {method_name}()"
            )
        
        method = getattr(self, method_name)
        
        # Verify it's callable
        if not callable(method):
            raise ValueError(
                f"Tool '{tool}' exists but is not callable in {self.name}"
            )
        
        # Call the tool method (support both sync and async)
        if asyncio.iscoroutinefunction(method):
            return await method(params)
        else:
            return method(params)
