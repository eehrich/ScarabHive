"""Markdown Formatter Plugin - Factory and exports.

Schema-based hook plugin for Markdown formatting and HTML conversion.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from .hooks import MarkdownFormatterPlugin


def PLUGIN_FACTORY(name: str, system_config: Any, server_config: Any) -> MarkdownFormatterPlugin:
    """Factory function to create plugin instance.
    
    Args:
        name: Plugin instance name
        system_config: System configuration
        server_config: instance-specific configuration (contains config from plugins.yaml)
        
    Returns:
        MarkdownFormatterPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return MarkdownFormatterPlugin(plugin_dir, server_config)
