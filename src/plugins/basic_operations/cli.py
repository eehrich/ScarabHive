"""Command-line interface for the BasicOperations plugin.

Provides simple commands to exercise the `wait` and `ping` tools locally for
development and debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from argparse import ArgumentParser, Namespace
from typing import Any

from .server import BasicOperationsServer

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def run_wait(args: Namespace) -> dict[str, Any]:
    server = BasicOperationsServer(name="cli_basic_ops", config={})

    params: dict[str, Any] = {
        "seconds": args.seconds,
        "update_interval": args.update_interval,
        "message": args.message,
    }

    try:
        result = await server.call("wait", params)
        return result
    except Exception as e:
        logger.error(f"Wait error: {e}")
        return {"error": str(e)}


async def run_ping(args: Namespace) -> dict[str, Any]:
    server = BasicOperationsServer(name="cli_basic_ops", config={})

    params: dict[str, Any] = {"include_details": args.details}

    try:
        result = await server.call("ping", params)
        return result
    except Exception as e:
        logger.error(f"Ping error: {e}")
        return {"error": str(e)}


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="BasicOperations plugin CLI",
        prog="python -m plugins.basic_operations.cli",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    wait_p = subparsers.add_parser("wait", help="Wait for a number of seconds with status updates")
    wait_p.add_argument("--seconds", "-s", type=float, required=True, help="Seconds to wait")
    wait_p.add_argument("--update-interval", "-u", type=float, default=1.0,
                        help="Status update interval in seconds (default: 1.0)")
    wait_p.add_argument("--message", "-m", default="CLI wait", help="Message to include in status updates")

    ping_p = subparsers.add_parser("ping", help="Quick ping probe")
    ping_p.add_argument("--details", "-d", action="store_true", help="Include server details in response")

    return parser


async def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "wait":
        result = await run_wait(args)
    elif args.command == "ping":
        result = await run_ping(args)
    else:
        logger.error(f"Unknown command: {args.command}")
        return 1

    print(json.dumps(result, indent=2))
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
