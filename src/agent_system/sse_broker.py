"""Packaged console wrapper for SSE broker inside the package namespace.

This exposes `agent_system.sse_broker:main` for entrypoint registration.
"""
from __future__ import annotations

from agent_system.mcp.sse_broker import run


def main() -> None:
    run()
