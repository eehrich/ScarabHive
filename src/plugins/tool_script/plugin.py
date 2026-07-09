"""Tool Script plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import ToolScriptServer

PLUGIN_FACTORY = ToolScriptServer
