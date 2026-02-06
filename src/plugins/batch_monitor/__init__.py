"""Batch Monitor Plugin.

Provides web UI for monitoring batch queue status.
"""

from .server import BatchMonitorWebFactory
from .plugin import PLUGIN_FACTORY

__all__ = ["BatchMonitorWebFactory", "PLUGIN_FACTORY"]
