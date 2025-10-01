"""Weather plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import WeatherServer


# MODERN: Use standardized plugin factory - automatically handles AgentConfig
PLUGIN_FACTORY = WeatherServer

