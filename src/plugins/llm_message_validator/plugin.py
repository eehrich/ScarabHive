"""LLM Message Validator Plugin - Factory and exports.

Schema-based hook plugin for message validation before LLM calls.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from .hooks import MessageValidatorPlugin


def PLUGIN_FACTORY() -> MessageValidatorPlugin:
    """Factory function to create plugin instance.
    
    Returns:
        MessageValidatorPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return MessageValidatorPlugin(plugin_dir)
