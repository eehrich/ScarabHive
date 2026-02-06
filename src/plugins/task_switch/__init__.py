"""Task Switch Plugin - State machine control for LLM agents."""

from .server import TaskSwitchServer

PLUGIN_FACTORY = TaskSwitchServer

__all__ = ["TaskSwitchServer", "PLUGIN_FACTORY"]
