"""Sequential Thinking plugin entrypoint (standardized pattern)."""

from __future__ import annotations

from .server import SequentialThinkingServer


PLUGIN_FACTORY = SequentialThinkingServer
