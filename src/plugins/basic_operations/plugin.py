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

from .server import BasicOperationsServer

logger = logging.getLogger(__name__)


def PLUGIN_FACTORY(name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True) -> BasicOperationsServer:
    """Create and configure a BasicOperations plugin server instance.
    
    Args:
        name: The plugin instance name (used for tool naming)
        config: Plugin configuration dictionary with optional settings:
            - max_wait_seconds (float): Maximum allowed wait time (default: 300)
            - default_update_interval (float): Default status update interval (default: 1.0)
        ssl_verify: Enable SSL certificate verification (default: True)
    
    Returns:
        Configured BasicOperationsServer instance
        
    Example:
        >>> server = PLUGIN_FACTORY("basic_ops", {"max_wait_seconds": 60})
        >>> # Creates server with 60 second maximum wait time
    """
    # Initialize with defaults if no config provided
    if config is None:
        config = {}
    
    # Log plugin creation
    logger.info(f"Creating BasicOperations plugin instance: {name}")
    logger.debug(f"Configuration: {config}")
    
    try:
        # Create and return the server instance
        server = BasicOperationsServer(name, config, ssl_verify)
        logger.info(f"BasicOperations plugin '{name}' created successfully")
        return server
        
    except Exception as e:
        logger.error(f"Failed to create BasicOperations plugin '{name}': {e}")
        raise


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