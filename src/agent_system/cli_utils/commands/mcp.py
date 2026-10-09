"""`agent-cli mcp`: inspect the external MCP servers -- list, status, test, tools.

Read-only, and nothing that holds state: a connection opened here dies with
this process. The command starts ``ToolServerIntegration``, runs one action
through the services the API serves as well (``ToolServerService``,
``ToolService.list_tools``) and shuts the integration down again.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from ...services import ToolServerService, ToolService
from ...tools.integration import ToolServerIntegration
from ..common import colorize, supports_color
from ..event_loop import run_async
from .table import print_table

logger = logging.getLogger(__name__)


async def _mcp_list_servers(tool_server_service: ToolServerService, args: Any) -> bool:
    """List configured external MCP servers using ToolServerService."""
    try:
        servers = await tool_server_service.list_servers()

        if args.out_format == "json":
            print(json.dumps(servers, indent=2, ensure_ascii=False))
        else:
            # Table format
            if not servers:
                print("No external MCP servers configured.")
                return True

            rows = []
            for server in servers:
                status = server.get("status", "unknown")

                if supports_color():
                    if status == "connected":
                        status = colorize("Connected", "32")  # green
                    elif status == "disconnected":
                        status = colorize("Disconnected", "31")  # red
                    else:
                        status = colorize("Disabled", "90")  # gray

                rows.append((
                    server["name"],
                    server.get("address", ""),
                    status,
                    server.get("description", "")
                ))

            print_table(rows, ["NAME", "ADDRESS", "STATUS", "DESCRIPTION"])

    except Exception as e:
        logger.exception("Failed to list tool servers: %s", e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return False
    return True


# The _mcp_* helpers answer whether the action succeeded; the caller turns a
# failure into exit code 1. They printed their error and exited 0, so a
# script could not tell `mcp test` of a dead server from a live one.
async def _mcp_status_servers(tool_server_service: ToolServerService, server_name: str | None, args: Any) -> bool:
    """Show status of external MCP servers using ToolServerService."""
    try:
        if server_name:
            status_info = await tool_server_service.get_server_status(server_name)
            if status_info is None:
                # The service answers None for an unknown name; this printed `null`
                status_info = {"error": f"Server '{server_name}' not found in configuration"}
            print(json.dumps(status_info, indent=2, ensure_ascii=False))
            return "error" not in status_info
        # Status for all servers - just list them
        return await _mcp_list_servers(tool_server_service, args)
    except Exception as e:
        logger.exception("Failed to get server status: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return False


async def _mcp_test_server(tool_server_service: ToolServerService, server_name: str, args: Any) -> bool:
    """Test connectivity and basic functionality of an external MCP server using ToolServerService."""
    try:
        result = await tool_server_service.test_server(server_name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return bool(result.get("success"))
    except Exception as e:
        logger.exception("Failed to test server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return False


async def _list_server_tools_via_service(tool_service: ToolService, server_name: str, args: Any) -> bool:
    """List all available tools for a server using ToolService."""
    try:
        result = await tool_service.list_tools(server_name, include_filtering=True)

        # An error has no tools to tabulate; the table used to report it as
        # "No tools available".
        if args.out_format == "table" and "error" not in result:
            print(f"\nServer: {server_name}")
            print("=" * (len(server_name) + 8))

            # ToolService names the key available_tools. Reading "tools" made
            # every server look empty in the table.
            available_tools = result.get("available_tools", [])
            if not available_tools:
                print("No tools available")
            else:
                # Only `blocked` filters an external server (ToolService.list_tools)
                blocked = set(result.get("filtering", {}).get("blocked_tools") or [])

                print(f"\nAvailable Tools ({len(available_tools)}):")
                print("-" * 30)
                for tool in available_tools:
                    print(f"  {tool}{' [BLOCKED]' if tool in blocked else ''}")

                effective = result.get("effective_tools", available_tools)
                print(f"\nEffective Tools ({len(effective)}):")
                print("-" * 30)
                for tool in effective:
                    print(f"  {tool}")
        else:
            # JSON format output
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return "error" not in result

    except Exception as e:
        logger.exception("Failed to list tools: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return False


async def _run_mcp_action(args: Any, config: Any) -> bool:
    """Start the tool integration, run the action, shut the integration down."""
    tool_integration = ToolServerIntegration(config=config)
    try:
        try:
            await tool_integration.initialize(config)
        except Exception as e:
            # Non-fatal: list/status still report the configuration
            logger.warning(f"tool integration initialization failed, continuing without live clients: {e}", exc_info=True)

        tool_server_service = ToolServerService(tool_integration, config)
        if args.action == "list":
            return await _mcp_list_servers(tool_server_service, args)
        if args.action == "status":
            return await _mcp_status_servers(tool_server_service, args.server, args)
        if args.action == "test":
            return await _mcp_test_server(tool_server_service, args.server, args)
        return await _list_server_tools_via_service(
            ToolService(tool_integration, config), args.server, args)
    finally:
        # Close the aiohttp sessions and stdio children the
        # initialization opened, however the action ended.
        try:
            await tool_integration.shutdown()
        except Exception as e:
            logger.debug(f"Error shutting down ToolServerIntegration: {e}")


def run_mcp_command(args: Any, config: Any) -> None:
    """Run an `mcp` action (a bare `mcp` prints its help in agent_cli); exit 1 when it failed."""
    try:
        ok = run_async(_run_mcp_action(args, config))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        ok = False
    if not ok:
        sys.exit(1)
