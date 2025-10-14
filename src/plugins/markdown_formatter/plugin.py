"""Markdown Formatter Plugin - Factory and exports.

Schema-based hook plugin for Markdown formatting and HTML conversion.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from .hooks import MarkdownFormatterPlugin


def PLUGIN_FACTORY() -> MarkdownFormatterPlugin:
    """Factory function to create plugin instance.
    
    Returns:
        MarkdownFormatterPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return MarkdownFormatterPlugin(plugin_dir)
