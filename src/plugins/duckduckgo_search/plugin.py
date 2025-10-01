"""DuckDuckGo search plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import DuckDuckGoSearchServer


PLUGIN_FACTORY = DuckDuckGoSearchServer

