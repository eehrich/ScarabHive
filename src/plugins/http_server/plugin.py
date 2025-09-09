"""HTTP server plugin entrypoint.

Unified factory pattern: expose PLUGIN_NAME and PLUGIN_FACTORY
with signature (name: str, config: dict|None = None, ssl_verify: bool = True)
returning the concrete server implementation. Lazy import keeps import-time
overhead minimal and avoids loading heavy dependencies unless instantiated.
"""

from __future__ import annotations

from typing import Any

from .server import HTTPServer  # lazy import


PLUGIN_FACTORY = HTTPServer

# Backward compatibility for tests/imports expecting create_plugin()
def create_plugin(name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True):  # pragma: no cover - shim
    return PLUGIN_FACTORY(name, config=config, ssl_verify=ssl_verify)


