"""Compatibility shim that re-exports the DateTimeServer implementation from the plugins package.

The real implementation lives in `plugins/datetime/server.py`. Keeping this shim allows
other modules to continue importing from `agent_system.servers.datetime.server`.
"""

from plugins.datetime.server import DateTimeServer

__all__ = ["DateTimeServer"]
