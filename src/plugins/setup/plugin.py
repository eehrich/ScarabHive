"""Setup plugin entry point: the setup agent's tools and the Setup panel."""
from __future__ import annotations

from .server import SetupServer

PLUGIN_FACTORY = SetupServer
