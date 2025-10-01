"""BasicOperations plugin factory and configuration.

This module provides the plugin factory function that creates and configures
the BasicOperations plugin server. It provides utility operations like wait,
echo, and ping functionality.
"""

from __future__ import annotations
import logging

from .server import BasicOperationsServer

logger = logging.getLogger(__name__)


PLUGIN_FACTORY = BasicOperationsServer
