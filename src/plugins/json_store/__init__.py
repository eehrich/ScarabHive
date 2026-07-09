"""JSON Store Plugin - validated JSON working documents for LLM agents."""

from .server import JsonStoreServer

PLUGIN_FACTORY = JsonStoreServer

__all__ = ["JsonStoreServer", "PLUGIN_FACTORY"]
