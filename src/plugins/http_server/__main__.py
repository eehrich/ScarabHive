"""HTTP Server Plugin CLI."""

import argparse
import asyncio
import sys

from agent_system.config.models import ToolServerConfig
from agent_system.config.settings import load_settings
from agent_system.plugins import discover_all_plugins
from agent_system.utils.logging import setup_logging

from .server import HTTPServer


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the HTTP server CLI."""
    parser = argparse.ArgumentParser(
        description="HTTP Server Tool plugin - FastAPI-based HTTP adapter for tool servers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Serve on the network: needs an API key (HTTP_SERVER_AUTH_KEY)
  HTTP_SERVER_AUTH_KEY=... python -m plugins.http_server --server-name datetime --host 0.0.0.0 --port 8000

  # Start with default settings
  python -m plugins.http_server --server-name web_scraper
        """
    )

    parser.add_argument(
        "--server-name",
        required=True,
        help="Name of the tool server to wrap with HTTP interface"
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the HTTP server to (default: 127.0.0.1)"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Port to bind the HTTP server to (default: 9000)"
    )

    parser.add_argument(
        "--config",
        help="Path to the main config file (default: AGENT_CONFIG_PATH, else config/config.yaml)"
    )

    parser.add_argument(
        "--no-ssl-verify",
        action="store_true",
        help="Disable SSL certificate verification"
    )

    parser.add_argument(
        "--version",
        action="version",
        version="HTTP Server Plugin 1.0.0"
    )

    return parser


def cli_main() -> None:
    """Main CLI entry point."""
    # Setup logging for proper color output
    setup_logging(True, "INFO", "logs/http_server.log")
    
    parser = build_parser()
    args = parser.parse_args()

    # The wrapped server gets its entry from plugins.yaml when there is one,
    # the way the tool registry builds it; otherwise --server-name is a plugin type.
    system_config = load_settings(args.config)
    if args.no_ssl_verify:
        system_config.network.ssl_verify = False
    entries = system_config.plugins.servers if system_config.plugins else {}
    server_config = entries.get(args.server_name) or ToolServerConfig(type=args.server_name, enabled=True)
    plugin_type = server_config.type or args.server_name

    # Discover available plugins
    plugins = discover_all_plugins()

    if plugin_type not in plugins:
        print(f"Error: Server '{args.server_name}' not found in available plugins:", file=sys.stderr)
        print(f"Available servers: {', '.join(sorted(plugins.keys()))}", file=sys.stderr)
        sys.exit(1)

    try:
        target_server = plugins[plugin_type](args.server_name, system_config, server_config)
    except Exception as e:
        print(f"Error creating server '{args.server_name}': {e}", file=sys.stderr)
        sys.exit(1)

    http_config = ToolServerConfig(type="http_server", enabled=True, host=args.host, port=args.port)
    http_server = HTTPServer("http_server", system_config, http_config)
    http_server.wrap_server(target_server)

    print(f"Starting HTTP server for tool server '{args.server_name}' on {args.host}:{args.port}")
    print(f"Health endpoint: http://{args.host}:{args.port}/health")
    print(f"Call endpoint: http://{args.host}:{args.port}/call")
    print("Press Ctrl+C to stop")

    try:
        asyncio.run(http_server.serve())
    except KeyboardInterrupt:
        print("\nHTTP server stopped")
    except Exception as e:
        print(f"Error running HTTP server: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
