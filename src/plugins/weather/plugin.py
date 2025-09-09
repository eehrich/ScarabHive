"""Weather plugin entrypoint (standardized)."""

from __future__ import annotations
from typing import Any

from .server import WeatherServer


PLUGIN_FACTORY = WeatherServer

