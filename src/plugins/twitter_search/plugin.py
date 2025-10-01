"""Twitter search plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import TwitterSearchServer


# MODERN: Use standardized plugin factory - automatically handles AgentConfig
PLUGIN_FACTORY = TwitterSearchServer


