"""Google search plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import GoogleSearchServer


PLUGIN_FACTORY = GoogleSearchServer

