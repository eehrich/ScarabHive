"""Schema-based MCP Server Base Class

Provides a base class for MCP servers that load their tool definitions
from schema.yaml files, eliminating code duplication across plugins.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING
import importlib.util

from .base import MCPServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class SchemaBasedMCPServer(MCPServer):
    """Base class for MCP servers that load tools from schema.yaml files.
    
    This class eliminates the need for every plugin to implement identical
    get_tools() methods that load and parse schema.yaml files.
    
    Plugins can simply inherit from this class and their schema.yaml file
    will be automatically loaded and parsed.
    
    The generic call() dispatcher in MCPServer will automatically route
    tool calls to methods matching the tool names.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
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
            logger.debug(f"Loaded schema for {self.name} plugin from {plugin_dir} with template_vars={template_vars}")
            return schema_data
            
        except Exception as e:
            raise RuntimeError(
                f"Failed to load schema for {self.name} plugin: {e}"
            ) from e
    
    def get_tools(self) -> list[dict[str, Any]]:
        """Load tools from the plugin's schema.yaml file.
        
        This method automatically loads and parses the schema.yaml file
        from the plugin's directory, eliminating the need for each plugin
        to implement this logic.
        
        Returns:
            List of tool definitions in OpenAI function format.
            
        Raises:
            RuntimeError: If schema is missing, invalid, or doesn't use multi-tool format.
        """
        if self._tools_cache is not None:
            return self._tools_cache
        
        schema_data = self._load_schema()
        
        # Support multi-tool format (recommended)
        if 'tools' in schema_data:
            tools = schema_data['tools']
            if not isinstance(tools, list):
                raise RuntimeError(
                    f"{self.name} plugin schema 'tools' must be a list"
                )
            self._tools_cache = tools
            return tools
        
        # Support legacy single-tool format for backward compatibility
        elif 'function' in schema_data:
            # Convert single function to multi-tool format
            tool = {
                "type": "function",
                "function": schema_data['function']
            }
            self._tools_cache = [tool]
            logger.debug(f"Converted single-tool format to multi-tool for {self.name}")
            return self._tools_cache
        
        else:
            raise RuntimeError(
                f"{self.name} plugin schema must contain either 'tools' array (recommended) "
                f"or 'function' object (legacy). Found keys: {list(schema_data.keys())}"
            )
    
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