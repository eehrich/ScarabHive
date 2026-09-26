"""stategraph plugin entry point: one server object with the tools and the panel."""

from .server import StateGraphServer

PLUGIN_FACTORY = StateGraphServer
