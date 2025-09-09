"""Web scraper plugin entrypoint (standardized)."""

from __future__ import annotations
from typing import Any

from .server import WebScraperServer


PLUGIN_FACTORY = WebScraperServer

