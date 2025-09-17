"""Plugin factory for the multi-tool test plugin."""
from __future__ import annotations

from typing import Any
from .server import MultiToolTestServer


def PLUGIN_FACTORY(name: str, config: dict[str, Any] | None = None) -> MultiToolTestServer:
    """Create and configure the multi-tool test server."""
    return MultiToolTestServer(name=name, config=config)