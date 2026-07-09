"""JSON Store plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import JsonStoreServer

# MODERN: Use standardized plugin factory
PLUGIN_FACTORY = JsonStoreServer
