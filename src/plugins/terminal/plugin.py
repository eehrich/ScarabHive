"""Terminal plugin factory and configuration.

This module provides the plugin factory function that creates and configures
the Terminal plugin server for secure bash command execution.
"""

from __future__ import annotations
import logging

from .server import TerminalServer

logger = logging.getLogger(__name__)


PLUGIN_FACTORY = TerminalServer
