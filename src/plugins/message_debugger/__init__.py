"""Message Debugger Plugin - Debug and inspect LLM conversation messages.

This plugin provides comprehensive debugging capabilities for LLM conversations,
capturing message snapshots before each LLM call for detailed inspection.
"""
from .plugin import PLUGIN_FACTORY

__all__ = ['PLUGIN_FACTORY']
