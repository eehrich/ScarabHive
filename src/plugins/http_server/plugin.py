"""HTTP server plugin entrypoint."""

from __future__ import annotations

from .server import HTTPServer


PLUGIN_FACTORY = HTTPServer


