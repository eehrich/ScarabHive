"""Terminal plugin - Secure bash command execution with process management."""

from .server import TerminalServer
from .plugin import PLUGIN_FACTORY

__all__ = ["TerminalServer", "PLUGIN_FACTORY"]
