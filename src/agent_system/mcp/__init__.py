"""
Model Context Protocol (MCP) Implementation for AgentSystem

This module provides MCP compatibility for the AgentSystem framework,
including both client and server functionality.

Key Components:
- MCPIntegration: Main integration class
- StandardMCPClient: MCP client implementation
- MCPHTTPServer: HTTP server exposing MCP endpoints
- PluginMCPAdapter: Adapts AgentSystem plugins to MCP interface
- MCPConfig: Configuration management for MCP settings
- MCPSecurityManager: Authentication and security handling
"""

from .integration import MCPIntegration
from .client import StandardMCPClient, MCPClientManager
from .http_server import MCPHTTPServer
from .plugin_adapter import PluginMCPAdapter, plugin_mcp_registry
from .core import MCPTool, MCPResource, MCPPrompt, MCPMessage, MCPError
from .config import MCPConfig, MCPConfigManager, create_example_config
from .security import MCPSecurityManager, configure_security

__all__ = [
    "MCPIntegration",
    "StandardMCPClient", 
    "MCPClientManager",
    "MCPHTTPServer",
    "PluginMCPAdapter",
    "plugin_mcp_registry",
    "MCPTool",
    "MCPResource", 
    "MCPPrompt",
    "MCPMessage",
    "MCPError",
    "MCPConfig",
    "MCPConfigManager",
    "create_example_config",
    "MCPSecurityManager",
    "configure_security"
]
