"""Datetime plugin entrypoint (standardized pattern)."""

from __future__ import annotations

from typing import Any

from .server import DateTimeServer  # lazy import


def _factory(name: str, cfg: dict[str, Any] | None = None, ssl_verify: bool = True):
    """Factory matching historical signature (name, cfg, ssl_verify)."""
    return DateTimeServer(name, config=cfg, ssl_verify=ssl_verify)

# Primary export used by plugin discovery
PLUGIN_FACTORY = _factory  # type: ignore

# Backward compatibility alias expected by existing tests
def factory(name: str, cfg: dict[str, Any] | None = None, ssl_verify: bool = True):  # pragma: no cover - shim
    return _factory(name, cfg=cfg, ssl_verify=ssl_verify)
