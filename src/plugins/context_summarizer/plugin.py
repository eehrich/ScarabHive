"""Context Summarizer Plugin - Factory and exports.

Hybrid plugin: Schema-based hooks + Web UI for viewing summarization history.
Hook implementations are in hooks.py, web endpoints in web_endpoints.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any

from .hooks import ContextSummarizerPlugin
from .web_endpoints import ContextSummarizerWebFactory


def PLUGIN_FACTORY() -> tuple[ContextSummarizerPlugin, ContextSummarizerWebFactory]:
    """Factory function to create hybrid plugin (hooks + web UI).
    
    Returns:
        Tuple of (hooks_plugin, web_factory)
    """
    plugin_dir = Path(__file__).parent
    
    # Shared history list for both hooks and web UI
    _summarization_history: List[Dict[str, Any]] = []
    
    # Create hooks plugin with history tracking
    hooks_plugin = ContextSummarizerPlugin(plugin_dir, summarization_history=_summarization_history)
    
    # Create web UI factory
    web_factory = ContextSummarizerWebFactory(_summarization_history)
    
    return (hooks_plugin, web_factory)
