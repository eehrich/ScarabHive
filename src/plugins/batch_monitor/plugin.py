"""Batch Monitor Plugin.

Provides web UI for monitoring batch queue status.
"""

from .server import BatchMonitorWebFactory

# Plugin factory for web-only plugin
PLUGIN_FACTORY = BatchMonitorWebFactory

__all__ = ["BatchMonitorWebFactory", "PLUGIN_FACTORY"]
