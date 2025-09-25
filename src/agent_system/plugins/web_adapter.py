"""
Plugin Web Interface and Registry

Provides web capabilities for AgentSystem plugins including endpoints,
static assets, and UI panels.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from pathlib import Path
from abc import ABC

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


class PluginWebInterface(ABC):
    """Base interface for plugins with web capabilities"""
    
    def get_web_router(self) -> Optional[APIRouter]:
        """Return FastAPI router with plugin endpoints
        
        Returns:
            Optional[APIRouter]: Router with plugin-specific endpoints,
                                or None if plugin has no web endpoints
        """
        return None
    
    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets (JS/CSS/HTML)
        
        Returns:
            Optional[Path]: Path to static assets directory,
                           or None if plugin has no static assets
        """
        return None
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Return list of UI panel configurations
        
        Returns:
            List[Dict[str, Any]]: List of panel configuration dictionaries.
                Each panel config should include:
                - id: unique panel identifier
                - title: display title for panel tab
                - url: URL for panel content (usually HTML page)
                - icon: icon name/class for panel tab (optional)
                - position: "left", "right", "bottom", "top" (optional, default: "right")
                - width: CSS width value (optional, default: "400px")
                - height: CSS height value (optional, default: "300px")
        """
        return []
    
    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration for endpoints
        
        Returns:
            Dict[str, Any]: Security configuration with keys:
                - require_auth: bool, whether endpoints require authentication
                - cors_origins: List[str], allowed CORS origins 
                - rate_limit: str, rate limit spec like "10/minute" (optional)
                - content_security_policy: str, CSP header value (optional)
        """
        return {
            "require_auth": False,
            "cors_origins": [],
            "rate_limit": None,
            "content_security_policy": None
        }


class PluginWebRegistry:
    """Registry for plugin web capabilities"""
    
    def __init__(self):
        self.web_plugins: Dict[str, PluginWebInterface] = {}
        self.active_routers: Dict[str, APIRouter] = {}
        self.static_mounts: Dict[str, Path] = {}
        self.security_configs: Dict[str, Dict[str, Any]] = {}
        self.plugin_metadata: Dict[str, Dict[str, Any]] = {}  # Store additional metadata
        logger.info("PluginWebRegistry initialized")
    
    def register_web_plugin(self, name: str, plugin, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Register a plugin's web capabilities
        
        Args:
            name: Plugin name/identifier
            plugin: Plugin instance implementing PluginWebInterface or having web methods
            metadata: Additional plugin metadata including schema path
        """
        logger.info(f"Registering web capabilities for plugin: {name}")
        
        self.web_plugins[name] = plugin
        
        # Store plugin metadata including schema path
        self.plugin_metadata[name] = metadata or {}
        
        # Try to find schema path if not provided
        if 'schema_path' not in self.plugin_metadata[name]:
            for plugin_dir in ["src/plugins", "plugins"]:
                for schema_name in ["schema.yaml", "mcp_schema.yaml", "schema.json", "mcp_schema.json"]:
                    schema_path = Path(plugin_dir) / name / schema_name
                    if schema_path.exists():
                        self.plugin_metadata[name]['schema_path'] = str(schema_path)
                        break
                if 'schema_path' in self.plugin_metadata[name]:
                    break
        
        # Register router if provided
        try:
            if hasattr(plugin, 'get_web_router'):
                router = plugin.get_web_router()
                if router:
                    self.active_routers[name] = router
                    logger.debug(f"Registered web router for plugin {name}")
        except Exception as e:
            logger.warning(f"Failed to get router for plugin {name}: {e}")
        
        # Register static assets if provided  
        try:
            if hasattr(plugin, 'get_static_assets'):
                static_path = plugin.get_static_assets()
                if static_path and static_path.exists():
                    self.static_mounts[name] = static_path
                    logger.debug(f"Registered static assets for plugin {name}: {static_path}")
        except Exception as e:
            logger.warning(f"Failed to get static assets for plugin {name}: {e}")
        
        # Store security configuration
        try:
            if hasattr(plugin, 'get_security_config'):
                security_config = plugin.get_security_config()
                self.security_configs[name] = security_config
                logger.debug(f"Registered security config for plugin {name}")
            else:
                self.security_configs[name] = {}
        except Exception as e:
            logger.warning(f"Failed to get security config for plugin {name}: {e}")
            self.security_configs[name] = {}
    
    def unregister_web_plugin(self, name: str) -> None:
        """Unregister a plugin's web capabilities
        
        Args:
            name: Plugin name/identifier
        """
        logger.info(f"Unregistering web capabilities for plugin: {name}")
        
        self.web_plugins.pop(name, None)
        self.active_routers.pop(name, None)
        self.static_mounts.pop(name, None)
        self.security_configs.pop(name, None)
    
    def get_all_panels(self) -> List[Dict[str, Any]]:
        """Get all registered UI panels
        
        Returns:
            List[Dict[str, Any]]: List of all panel configurations from all plugins
        """
        panels = []
        for name, plugin in self.web_plugins.items():
            try:
                if hasattr(plugin, 'get_panels'):
                    plugin_panels = plugin.get_panels()
                    for panel in plugin_panels:
                        # Ensure panel has required fields and add plugin metadata
                        panel_config = dict(panel)
                        panel_config["plugin_name"] = name
                        
                        # Validate required fields
                        if "id" not in panel_config:
                            logger.warning(f"Panel from plugin {name} missing 'id' field, skipping")
                            continue
                        if "title" not in panel_config:
                            panel_config["title"] = f"Plugin {name}"
                        if "url" not in panel_config:
                            logger.warning(f"Panel {panel_config['id']} from plugin {name} missing 'url' field, skipping")
                            continue
                        
                        # Set defaults for optional fields
                        panel_config.setdefault("position", "right")
                        panel_config.setdefault("width", "400px")
                        panel_config.setdefault("height", "300px")
                        
                        panels.append(panel_config)
                        
            except Exception as e:
                logger.warning(f"Failed to get panels for plugin {name}: {e}")
        
        return panels
    
    def get_security_config(self, plugin_name: str) -> Dict[str, Any]:
        """Get security configuration for a specific plugin
        
        Args:
            plugin_name: Name of the plugin
            
        Returns:
            Dict[str, Any]: Security configuration for the plugin
        """
        return self.security_configs.get(plugin_name, {})
    
    def apply_to_app(self, app: FastAPI) -> None:
        """Apply all registered web capabilities to FastAPI app
        
        Args:
            app: FastAPI application instance
        """
        logger.info("Applying plugin web capabilities to FastAPI app")
        
        # Include plugin routers
        for name, router in self.active_routers.items():
            try:
                app.include_router(router, tags=[f"plugin-{name}"])
                logger.info(f"Mounted web router for plugin {name}")
            except Exception as e:
                logger.error(f"Failed to mount router for plugin {name}: {e}")
        
        # Mount static assets
        for name, path in self.static_mounts.items():
            try:
                mount_path = f"/plugins/{name}/static"
                app.mount(mount_path, 
                         StaticFiles(directory=str(path)), 
                         name=f"plugin-{name}-static")
                logger.info(f"Mounted static assets for plugin {name} at {mount_path}")
            except Exception as e:
                logger.error(f"Failed to mount static files for plugin {name}: {e}")
        
        # Add panel discovery endpoint
        @app.get("/api/plugins/panels")
        def get_plugin_panels():
            """Get all available plugin UI panels"""
            try:
                panels = self.get_all_panels()
                logger.debug(f"Returning {len(panels)} plugin panels")
                return {"panels": panels}
            except Exception as e:
                logger.error(f"Failed to get plugin panels: {e}")
                return {"panels": [], "error": str(e)}
        
        logger.info(f"Applied web capabilities for {len(self.web_plugins)} plugins")


# Global plugin web registry instance
plugin_web_registry = PluginWebRegistry()

def get_web_plugin_registry() -> Optional[Dict[str, Any]]:
    """Get the web plugin registry for API access"""
    global plugin_web_registry
    if not plugin_web_registry:
        return None
    
    # Return plugin information with schema paths
    registry_data = {}
    for name, plugin in plugin_web_registry.web_plugins.items():
        metadata = plugin_web_registry.plugin_metadata.get(name, {})
        registry_data[name] = {
            'name': getattr(plugin, 'name', name),
            'description': getattr(plugin, 'description', ''),
            'schema_path': metadata.get('schema_path'),
            **metadata  # Include any additional metadata
        }
    
    return registry_data