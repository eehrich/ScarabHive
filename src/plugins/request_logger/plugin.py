"""Request Logger Plugin - Factory and exports.

Schema-based hooks-only plugin that logs agent requests and responses.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from .hooks import RequestLoggerPlugin


def PLUGIN_FACTORY() -> RequestLoggerPlugin:
    """Factory function to create plugin instance.
    
    Returns:
        RequestLoggerPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return RequestLoggerPlugin(plugin_dir)
