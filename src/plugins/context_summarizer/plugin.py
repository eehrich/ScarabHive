"""Context Summarizer Plugin - Factory and exports.

Schema-based hooks plugin for intelligent context reduction using LLM.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from .hooks import ContextSummarizerPlugin


def PLUGIN_FACTORY() -> ContextSummarizerPlugin:
    """Factory function to create plugin instance.
    
    Returns:
        ContextSummarizerPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return ContextSummarizerPlugin(plugin_dir)
