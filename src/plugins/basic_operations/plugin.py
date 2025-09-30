"""BasicOperations plugin factory and configuration.

This module provides the plugin factory function that creates and configures
the BasicOperations plugin server. It provides utility operations like wait,
echo, and ping functionality.
"""

from __future__ import annotations
import logging
from typing import Any
from pathlib import Path

import yaml
from agent_system.plugins.factory_utils import make_agent_plugin_factory

from .server import BasicOperationsServer

logger = logging.getLogger(__name__)


# MODERN: Use standardized plugin factory - automatically handles AgentConfig
PLUGIN_FACTORY = make_agent_plugin_factory(BasicOperationsServer)


# Plugin metadata for discovery is loaded from plugin.yaml when available.
def _load_plugin_info() -> dict[str, Any]:
    try:
        plugin_dir = Path(__file__).parent
        yaml_path = plugin_dir / "plugin.yaml"
        if yaml_path.exists():
            with yaml_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
                # Ensure keys exist
                return data or {}
    except Exception:
        logger.debug("Failed to load plugin.yaml for basic_operations; falling back to inline metadata")

    # Fallback inline metadata
    return {
        "name": "basic_operations",
        "version": "1.0.0",
        "description": "Basic utility operations including wait and ping",
        "author": "AgentSystem Team",
        "tools": [
            {"name": "wait", "description": "Wait for specified seconds with countdown status"},
            {"name": "ping", "description": "Simple connectivity test with timestamp"},
        ],
        "config_schema": {
            "type": "object",
            "properties": {
                "max_wait_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 86400,
                    "default": 3600,
                    "description": "Maximum allowed wait time in seconds",
                },
                "default_update_interval": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 60,
                    "default": 1.0,
                    "description": "Default interval between status updates in seconds",
                },
            },
            "additionalProperties": False,
        },
    }


PLUGIN_INFO = _load_plugin_info()