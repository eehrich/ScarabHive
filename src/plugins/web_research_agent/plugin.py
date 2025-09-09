"""Web research agent plugin entrypoint.

Provides a PLUGIN_FACTORY with historical (name, cfg, ssl_verify) signature
so bootstrap discovery and tests can instantiate consistently.
"""

from __future__ import annotations
from typing import Any

from .server import WebResearchAgent as WebResearchAgentServer


def _factory(name: str, cfg: dict[str, Any] | None = None, ssl_verify: bool = True):
	return WebResearchAgentServer(name, config=cfg, ssl_verify=ssl_verify)

PLUGIN_FACTORY = _factory  # discovery export

# Backwards compatibility: some tests import PLUGIN_FACTORY and call with (name, cfg, ssl_verify)
def factory(name: str, cfg: dict[str, Any] | None = None, ssl_verify: bool = True):  # pragma: no cover
	return _factory(name, cfg=cfg, ssl_verify=ssl_verify)


