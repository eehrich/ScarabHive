"""
Model Context Protocol (MCP) Implementation for AgentSystem

This module provides MCP compatibility for the AgentSystem framework,
including both client and server functionality.

Key Components:
- MCPIntegration: Main integration class
- StandardMCPClient: MCP client implementation
- PluginMCPAdapter: Available from agent_system.plugins package
- MCPSecurityManager: Authentication and security handling
"""

from .integration import MCPIntegration
from .client import StandardMCPClient, MCPClientManager
from .core import MCPTool, MCPResource, MCPPrompt, MCPMessage, MCPError
from .security import MCPSecurityManager, configure_security
from .schema_based import SchemaBasedMCPServer

__all__ = [
    "MCPIntegration",
    "StandardMCPClient",
    "MCPClientManager",
    "MCPTool",
    "MCPResource",
    "MCPPrompt",
    "MCPMessage",
    "MCPError",
    "MCPSecurityManager",
    "configure_security",
    "SchemaBasedMCPServer"
]

