"""Command-line interface for the example plugin.

This module provides CLI commands for testing and interacting with the example
plugin tools directly from the command line, useful for development and debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from argparse import ArgumentParser, Namespace
from typing import Any

from .server import ExampleServer

# Configure logging for CLI
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def run_calculator(args: Namespace) -> dict[str, Any]:
    """Run calculator command.
    
    Args:
        args: Command line arguments
        
    Returns:
        Calculator operation result
    """
    server = ExampleServer(name="cli", config={"precision": args.precision})
    
    params = {
        "operation": args.operation,
        "a": args.a,
        "b": args.b
    }
    
    try:
        result = await server.call("cli_calculator", params)
        return result
    except Exception as e:
        logger.error(f"Calculator error: {e}")
        return {"error": str(e)}


async def run_formatter(args: Namespace) -> dict[str, Any]:
    """Run formatter command.
    
    Args:
        args: Command line arguments
        
    Returns:
        Text formatting result
    """
    server = ExampleServer(name="cli", config={"max_text_length": args.max_length})
    
    params = {
        "text": args.text,
        "format": args.format
    }
    
    try:
        result = await server.call("cli_formatter", params)
        return result
    except Exception as e:
        logger.error(f"Formatter error: {e}")
        return {"error": str(e)}


async def run_status(args: Namespace) -> dict[str, Any]:
    """Run status command.
    
    Args:
        args: Command line arguments
        
    Returns:
        Plugin status information
    """
    config = {}
    if hasattr(args, 'precision'):
        config["precision"] = args.precision
    if hasattr(args, 'max_length'):
        config["max_text_length"] = args.max_length
    if hasattr(args, 'debug'):
        config["enable_debug"] = args.debug
        
    server = ExampleServer(name="cli", config=config)
    
    params = {"verbose": args.verbose}
    
    try:
        result = await server.call("cli_status", params)
        return result
    except Exception as e:
        logger.error(f"Status error: {e}")
        return {"error": str(e)}


def create_parser() -> ArgumentParser:
    """Create command line parser.
    
    Returns:
        Configured argument parser
    """
    parser = ArgumentParser(
        description="Example plugin CLI for testing and development",
        prog="python -m plugins.example.cli"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Calculator command
    calc_parser = subparsers.add_parser("calculator", help="Perform arithmetic operations")
    calc_parser.add_argument("--operation", "-o", 
                           choices=["add", "subtract", "multiply", "divide"],
                           required=True, help="Arithmetic operation")
    calc_parser.add_argument("--a", "-a", type=float, required=True, help="First number")
    calc_parser.add_argument("--b", "-b", type=float, required=True, help="Second number")
    calc_parser.add_argument("--precision", "-p", type=int, default=2, 
                           help="Decimal precision (default: 2)")
    
    # Formatter command
    fmt_parser = subparsers.add_parser("formatter", help="Format text in various ways")
    fmt_parser.add_argument("--text", "-t", required=True, help="Text to format")
    fmt_parser.add_argument("--format", "-f",
                          choices=["uppercase", "lowercase", "title", "reverse"],
                          required=True, help="Format to apply")
    fmt_parser.add_argument("--max-length", "-m", type=int, default=1000,
                          help="Maximum text length (default: 1000)")
    
    # Status command
    status_parser = subparsers.add_parser("status", help="Get plugin status")
    status_parser.add_argument("--verbose", "-v", action="store_true",
                             help="Show detailed status information")
    status_parser.add_argument("--precision", "-p", type=int, default=2,
                             help="Calculator precision setting")
    status_parser.add_argument("--max-length", "-m", type=int, default=1000,
                             help="Formatter max length setting")
    status_parser.add_argument("--debug", "-d", action="store_true",
                             help="Enable debug mode")
    
    return parser


async def main() -> int:
    """Main CLI entry point.
    
    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Route to appropriate command handler
    if args.command == "calculator":
        result = await run_calculator(args)
    elif args.command == "formatter":
        result = await run_formatter(args)
    elif args.command == "status":
        result = await run_status(args)
    else:
        logger.error(f"Unknown command: {args.command}")
        return 1
    
    # Print result as JSON
    print(json.dumps(result, indent=2))
    
    # Return error code if result contains error
    return 1 if "error" in result else 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)