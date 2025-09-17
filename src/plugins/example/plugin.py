"""Example plugin exposing a small multi-tool MCPServer for demos and tests."""

from __future__ import annotations
from typing import Any

from .server import MultiToolTestServer


def PLUGIN_FACTORY(name: str, config: dict[str, Any] | None = None) -> MultiToolTestServer:
    return MultiToolTestServer(name=name, config=config)

