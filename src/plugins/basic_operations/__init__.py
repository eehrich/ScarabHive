"""BasicOperations plugin package initialization.

This package provides essential utility operations for the AgentSystem,
including timing, waiting, and basic system operations.
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "AgentSystem Team"
__description__ = "Basic utility operations including wait and ping functions"

# Export main components for easier importing
from .server import BasicOperationsServer
from .plugin import PLUGIN_FACTORY

__all__ = ["BasicOperationsServer", "PLUGIN_FACTORY"]