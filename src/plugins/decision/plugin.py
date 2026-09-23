"""Entrypoint for the Decision plugin."""

from .server import DecisionServer

PLUGIN_FACTORY = DecisionServer

__all__ = ["DecisionServer", "PLUGIN_FACTORY"]
