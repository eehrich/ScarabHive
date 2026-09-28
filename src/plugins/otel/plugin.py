"""otel plugin -- factory. The hooks are in hooks.py, the exporters in telemetry.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .hooks import OtelHooks


def PLUGIN_FACTORY(name: Any = None, system_config: Any = None, server_config: Any = None) -> OtelHooks:
    """Called by plugin discovery as (name, system_config, server_config)."""
    return OtelHooks(Path(__file__).parent, server_config)
