"""Schema-based Web Plugin Interface Base Class

Provides a base class for plugins with web interfaces that load their
configuration from schema.yaml files, eliminating code duplication.

This follows the same pattern as SchemaBasedMCPServer and SchemaBasedPluginHook.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_system.core.schema_base_mixin import SchemaBaseMixin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class SchemaBasedPluginWebInterface(SchemaBaseMixin):
    """Base class for plugins with web interfaces that load from schema.yaml files.
    
    This class eliminates the need for web-only and hook+web plugins to implement
    identical get_schema_data() methods that load schema.yaml files.
    
    Plugins can simply inherit from this class and their schema.yaml file will be
    automatically loaded and available for web UI configuration.
    
    Example:
        class MyWebPlugin(SchemaBasedPluginWebInterface):
            def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
                super().__init__(name, system_config, mcp_config)
            
            def get_web_router(self):
                # Return FastAPI router for web endpoints
                pass
    
    Note: This class uses SchemaBaseMixin for consistent schema loading across
    all schema-based components (MCP servers, agents, hooks, web interfaces).
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Initialize schema-based web interface plugin.
        
        Args:
            name: Plugin name
            system_config: Global system configuration
            mcp_config: MCP server configuration (may be empty for web-only plugins)
        """
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Initialize schema base
        self._init_schema_base()
