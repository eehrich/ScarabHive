"""Tavily search plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import TavilySearchServer


PLUGIN_FACTORY = TavilySearchServer
