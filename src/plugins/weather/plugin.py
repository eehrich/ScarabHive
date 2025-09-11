"""Weather plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import WeatherServer


PLUGIN_FACTORY = WeatherServer

