"""HTTP Server Plugin Factory."""

from typing import Any

from .server import HTTPServer


def create_plugin(name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True) -> HTTPServer:
    """Create an HTTP server plugin instance.

    Args:
        name: Plugin name
        config: Plugin configuration
        ssl_verify: Whether to verify SSL certificates

    Returns:
        HTTPServer instance
    """
    return HTTPServer(name, config, ssl_verify)


# Plugin discovery interface
PLUGIN_NAME = "http_server"
PLUGIN_FACTORY = create_plugin
