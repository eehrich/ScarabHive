"""Tool Script Plugin - scripted tool chains without LLM round-trips."""

from .server import ToolScriptServer, ToolCallError

PLUGIN_FACTORY = ToolScriptServer

__all__ = ["ToolScriptServer", "ToolCallError", "PLUGIN_FACTORY"]
