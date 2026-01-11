"""Task Switch plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import TaskSwitchServer

# MODERN: Use standardized plugin factory
PLUGIN_FACTORY = TaskSwitchServer
