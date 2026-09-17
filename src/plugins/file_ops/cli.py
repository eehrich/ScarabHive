"""Command-line interface for the file_ops plugin.

Provides commands to exercise file operation tools locally for development and debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, ToolServerConfig
from .server import FileOpsServer

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def create_test_server(allowed_dirs: list[str]) -> FileOpsServer:
    """Create a test file operations server with mock configs."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(Path.cwd())

    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = allowed_dirs
    server_config.search = {"enable_indexing": False}  # Disable for CLI testing

    return FileOpsServer("cli_file_ops", system_config, server_config)


async def cmd_read(args: Namespace) -> dict[str, Any]:
    """Read file contents."""
    server = create_test_server([str(Path(args.file_path).parent.absolute())])

    params: dict[str, Any] = {
        "filePath": str(Path(args.file_path).absolute()),
    }

    if args.offset:
        params["offset"] = args.offset
    if args.limit:
        params["limit"] = args.limit

    try:
        result = await server.read_file(params)
        return result
    except Exception as e:
        logger.error(f"Read error: {e}")
        return {"error": str(e)}


async def cmd_list(args: Namespace) -> dict[str, Any]:
    """List directory contents."""
    server = create_test_server([str(Path(args.dir_path).absolute())])

    params: dict[str, Any] = {
        "dir_path": str(Path(args.dir_path).absolute()),
        "recursive": args.recursive,
        "include_hidden": args.include_hidden,
    }

    if args.pattern:
        params["pattern"] = args.pattern

    try:
        result = await server.list_directory(params)
        return result
    except Exception as e:
        logger.error(f"List error: {e}")
        return {"error": str(e)}


async def cmd_search(args: Namespace) -> dict[str, Any]:
    """Search for files by name pattern."""
    search_dir = Path(args.search_dir or ".").absolute()
    server = create_test_server([str(search_dir)])

    params: dict[str, Any] = {
        "pattern": args.pattern,
    }

    if args.max_results:
        params["max_results"] = args.max_results

    try:
        result = await server.search_files(params)
        return result
    except Exception as e:
        logger.error(f"Search error: {e}")
        return {"error": str(e)}


async def cmd_grep(args: Namespace) -> dict[str, Any]:
    """Search file contents with grep."""
    search_dir = Path(args.search_dir or ".").absolute()
    server = create_test_server([str(search_dir)])

    params: dict[str, Any] = {
        "query": args.query,
        "is_regex": args.regex,
        "case_sensitive": args.case_sensitive,
    }

    if args.context:
        params["context_lines"] = args.context
    if args.include_pattern:
        params["include_pattern"] = args.include_pattern
    if args.max_results:
        params["max_results"] = args.max_results

    try:
        result = await server.grep_search(params)
        return result
    except Exception as e:
        logger.error(f"Grep error: {e}")
        return {"error": str(e)}


def create_parser() -> ArgumentParser:
    """Create argument parser for all commands."""
    parser = ArgumentParser(
        description="File Operations plugin CLI",
        prog="python -m plugins.file_ops",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Read command
    read_p = subparsers.add_parser("read", help="Read file contents")
    read_p.add_argument("file_path", help="Path to file to read")
    read_p.add_argument("--offset", type=int, help="Starting line number (0-indexed)")
    read_p.add_argument("--limit", type=int, help="Maximum number of lines to read")

    # List command
    list_p = subparsers.add_parser("list", help="List directory contents")
    list_p.add_argument("dir_path", help="Path to directory")
    list_p.add_argument("--recursive", action="store_true", help="Recursive listing")
    list_p.add_argument("--pattern", help="Filter pattern (glob)")
    list_p.add_argument("--include-hidden", action="store_true", help="Include hidden files")

    # Search command
    search_p = subparsers.add_parser("search", help="Search for files by name")
    search_p.add_argument("pattern", help="Search pattern (glob)")
    search_p.add_argument("--search-dir", help="Directory to search in (default: current)")
    search_p.add_argument("--max-results", type=int, help="Maximum results to return")

    # Grep command
    grep_p = subparsers.add_parser("grep", help="Search file contents")
    grep_p.add_argument("query", help="Search query")
    grep_p.add_argument("--search-dir", help="Directory to search in (default: current)")
    grep_p.add_argument("--regex", action="store_true", help="Use regex search")
    grep_p.add_argument("--case-sensitive", action="store_true", help="Case-sensitive search")
    grep_p.add_argument("--context", type=int, help="Lines of context to show")
    grep_p.add_argument("--include-pattern", help="File pattern to search in")
    grep_p.add_argument("--max-results", type=int, help="Maximum results to return")

    return parser


async def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Dispatch to appropriate command
    command_map = {
        "read": cmd_read,
        "list": cmd_list,
        "search": cmd_search,
        "grep": cmd_grep,
    }

    cmd_func = command_map.get(args.command)
    if not cmd_func:
        logger.error(f"Unknown command: {args.command}")
        return 1

    result = await cmd_func(args)
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
