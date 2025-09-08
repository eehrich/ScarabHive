"""HTTP Server Plugin CLI."""

import argparse
import asyncio
import sys
from typing import Any

from agent_system.mcp.plugins import discover_all_plugins

from .server import HTTPServer


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the HTTP server CLI."""
    parser = argparse.ArgumentParser(
        description="HTTP Server MCP Plugin - FastAPI-based HTTP adapter for MCP servers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start HTTP server for a specific MCP server
  python -m plugins.http_server --server-name llm_router --host 0.0.0.0 --port 8000

  # Start with default settings
  python -m plugins.http_server --server-name web_scraper
        """
    )

    parser.add_argument(
        "--server-name",
        required=True,
        help="Name of the MCP server to wrap with HTTP interface"
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
        help="Path to configuration file"
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
    parser = build_parser()
    args = parser.parse_args()

    # Discover available plugins
    plugins = discover_all_plugins()

    if args.server_name not in plugins:
        print(f"Error: Server '{args.server_name}' not found in available plugins:", file=sys.stderr)
        print(f"Available servers: {', '.join(sorted(plugins.keys()))}", file=sys.stderr)
        sys.exit(1)

    # Create the target server
    server_factory = plugins[args.server_name]
    server_config = {}  # Could be loaded from config file if provided

    try:
        target_server = server_factory(args.server_name, server_config, ssl_verify=not args.no_ssl_verify)
    except Exception as e:
        print(f"Error creating server '{args.server_name}': {e}", file=sys.stderr)
        sys.exit(1)

    # Create HTTP server wrapper
    http_config = {
        "host": args.host,
        "port": args.port
    }
    http_server = HTTPServer("http_server", http_config, ssl_verify=not args.no_ssl_verify)
    http_server.wrap_server(target_server)

    print(f"Starting HTTP server for MCP server '{args.server_name}' on {args.host}:{args.port}")
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
