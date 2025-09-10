"""Twitter search plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import TwitterSearchServer


PLUGIN_FACTORY = TwitterSearchServer


