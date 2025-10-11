"""Configuration package for the Agent System.

This package provides configuration loading and model definitions.
"""

from .models import (
    AgentSystemConfig,
    AgentConfig,
    LLMSystemConfig,
    MCPSystemConfig,
    MCPConfig,
    PluginsConfig,
    MCPServersConfig,
    MCPServerModeConfig,
)
from .settings import load_settings, get_mcp_config_by_name

__all__ = [
    "AgentSystemConfig",
    "AgentConfig",
    "LLMSystemConfig",
    "MCPSystemConfig",  # DEPRECATED - use PluginsConfig, MCPServersConfig instead
    "MCPConfig",
    "PluginsConfig",
    "MCPServersConfig",
    "MCPServerModeConfig",
    "load_settings",
    "get_mcp_config_by_name",
]