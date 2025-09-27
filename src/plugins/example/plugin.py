"""Example plugin factory and configuration.

This module provides the plugin factory function that creates and configures
the example plugin server. It demonstrates proper plugin initialization,
configuration handling, and dependency injection patterns.
"""

from __future__ import annotations
import logging
from typing import Any

from .server import ExampleServer

logger = logging.getLogger(__name__)


def PLUGIN_FACTORY(name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True) -> ExampleServer:
    """Create and configure an example plugin server instance.
    
    Args:
        name: The plugin instance name (used for tool naming)
        config: Plugin configuration dictionary with optional settings:
            - precision (int): Decimal precision for calculations (default: 2)
            - max_text_length (int): Maximum text length for formatting (default: 1000)
            - enable_debug (bool): Enable debug logging (default: False)
        ssl_verify: Enable SSL certificate verification (default: True)
    
    Returns:
        Configured ExampleServer instance
        
    Example:
        >>> plugin = PLUGIN_FACTORY("example", {"precision": 3, "enable_debug": True})
        >>> tools = await plugin.list_tools()
        >>> len(tools)
        3
    """
    logger.debug(f"Creating example plugin instance: {name}")
    
    # Apply configuration defaults
    config = config or {}
    config.setdefault("precision", 2)
    config.setdefault("max_text_length", 1000)
    config.setdefault("enable_debug", False)
    
    # Configure logging if debug is enabled
    if config.get("enable_debug"):
        logging.getLogger(f"plugins.example.{name}").setLevel(logging.DEBUG)
        logger.debug(f"Debug logging enabled for {name}")
    
    # Validate configuration
    try:
        precision = int(config["precision"])
        if not 0 <= precision <= 10:
            raise ValueError("precision must be between 0 and 10")
        
        max_length = int(config["max_text_length"])
        if not 1 <= max_length <= 100000:
            raise ValueError("max_text_length must be between 1 and 100000")
            
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid configuration for {name}: {e}")
        raise ValueError(f"Invalid plugin configuration: {e}") from e
    
    logger.info(f"Example plugin '{name}' configured with precision={precision}, "
                f"max_text_length={max_length}, debug={config['enable_debug']}")
    
    return ExampleServer(name=name, config=config, ssl_verify=ssl_verify)

