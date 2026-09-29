"""Base Schema Mixin - Common schema loading for all schema-based components.

This base mixin provides ONLY schema loading functionality without any
assumptions about tools, hooks, or web UI. It's the foundation for:
- SchemaBasedToolMixin (tool servers & agents with tools)
- SchemaBasedHookMixin (hook plugins)
- SchemaBasedWebMixin (web UI plugins)
"""
from __future__ import annotations

import logging
import importlib.util
from pathlib import Path
from typing import Any

from agent_system.paths import relocate_data_paths

logger = logging.getLogger(__name__)


class SchemaBaseMixin:
    """Base mixin for schema loading without tool/hook/web assumptions.
    
    This provides only the core schema loading and caching functionality.
    Subclasses add specific functionality for tools, hooks, or web UI.
    
    Classes using this mixin should:
    1. Call _init_schema_base() in __init__
    2. Have a 'name' attribute
    3. Optionally override get_template_vars() for custom variables
    """
    
    # Type hints for attributes that will be provided by subclasses
    name: str
    
    def _init_schema_base(self) -> None:
        """Initialize schema base caches.
        
        Must be called in __init__ of classes using this mixin.
        """
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
                    f"Missing or invalid schema.yaml for {self.name} in {plugin_dir}"
                )
            
            self._schema_cache = schema_data
            logger.debug(
                f"Loaded schema for {self.name} from {plugin_dir} "
                f"with template_vars={template_vars}"
            )
            return schema_data
            
        except Exception as e:
            raise RuntimeError(
                f"Failed to load schema for {self.name}: {e}"
            ) from e
    
    def get_schema_data(self) -> dict[str, Any]:
        """Get the full loaded schema data.
        
        This method provides access to the complete schema data.
        
        Returns:
            The complete parsed schema data.
        """
        return self._load_schema()
    
    def clear_schema_cache(self) -> None:
        """Clear the cached schema data.
        
        This forces the schema to be reloaded on the next access.
        Useful for development and testing.
        """
        self._schema_cache = None
        logger.debug(f"Cleared schema cache for {self.name}")


def config_defaults_from_schema(schema_config: dict[str, Any] | None) -> dict[str, Any]:
    """The values behind a schema's ``config:`` block.

    A schema writes each key as ``{type, default, description}``; a plugin
    wants ``{key: default}``. Plain values are passed through, so a schema
    that writes the short form keeps working.

    Lives here because both halves of the plugin API need it and neither may
    depend on the other: the hook base (``hooks.schema_based``) and the base
    for a plugin that is a tool server as well as a hook
    (``tools.hook_tool_server``). A third copy had already grown inside the
    todo plugin and drifted into ignoring the plugins.yaml block.

    A default under ``data/`` moves with the data directory, as the same value
    in plugins.yaml would (paths.py).
    """
    values: dict[str, Any] = {}
    for key, value in (schema_config or {}).items():
        if isinstance(value, dict) and "default" in value:
            values[key] = value["default"]
        else:
            values[key] = value
    return relocate_data_paths(values)
