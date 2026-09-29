"""Example plugin package initialization.

This package provides a comprehensive example of plugin development
for the AgentSystem, demonstrating best practices and common patterns.
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "AgentSystem Team"
__description__ = "Reference implementation for plugin development"

# Export main components for easier importing
from .server import ExampleServer
from .plugin import PLUGIN_FACTORY

__all__ = ["ExampleServer", "PLUGIN_FACTORY"]