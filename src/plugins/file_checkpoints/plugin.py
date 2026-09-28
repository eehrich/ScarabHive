"""file_checkpoints -- factory.

A hook plugin: two tool hooks record the files an agent changes, and the
plugin registers itself as the file rewinder the chat's /undo files and
/rewind ask (agent_system.file_rewind).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .hooks import FileCheckpointsPlugin


def PLUGIN_FACTORY(name: Optional[str] = None, system_config: Any = None,
                   server_config: Any = None) -> FileCheckpointsPlugin:
    """Called as factory(name, system_config, server_config)."""
    return FileCheckpointsPlugin(Path(__file__).parent, name=name or "file_checkpoints",
                                 server_config=server_config)
