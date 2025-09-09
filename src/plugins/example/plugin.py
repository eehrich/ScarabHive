"""Example plugin (kept simple but aligned to standard factory pattern)."""

from __future__ import annotations
from typing import Any



class ExampleServer:
    def __init__(self, name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True):
        self.name = name
        self.config = config or {}
        self.ssl_verify = ssl_verify

    async def call(self, *args, **kwargs):  # pragma: no cover - trivial
        return {"status": "ok", "name": self.name}


PLUGIN_FACTORY = ExampleServer

