"""Cognitive Stack Plugin - Working memory for LLMs.

Provides a stack-based working memory tool for managing nested contexts,
interrupt-and-resume patterns, and hierarchical problem-solving.
"""

from .plugin import PLUGIN_FACTORY

__all__ = ["PLUGIN_FACTORY"]
