from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml
from typing import Any, Dict, List, Tuple, Union, Optional

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

from .config.settings import load_settings
from .config.models import AgentSystemConfig
from .llm.models import ChatMessage
from .plugins import discover_all_plugins
from .mcp.base import MCPRegistry
from .mcp.status import status_bus
from .mcp.integration import MCPIntegration, initialize_mcp, shutdown_mcp
from .llm.batch.initialization import init_batch_system, shutdown_batch_system
from .utils.logging import setup_logging
from .servers.agent.server import Agent

# Import services
from .services import MCPService, ToolService
from .services.session_manager import SessionPermissionError
from .cli_utils.common import (
    supports_color as _supports_color,
    colorize as _colorize,
    set_color_mode,
    format_output_with_hooks,
    render_with_rich
)
from .cli_utils.commands.hooks import handle_hooks_command


def _get_plugins_config(config: AgentSystemConfig):
    """Get plugins configuration."""
    return config.plugins


def _get_server_mode_config(config: AgentSystemConfig):
    """Get server_mode configuration."""
    return config.server_mode


logger = logging.getLogger(__name__)


async def _maybe_await_get_client(mcp_integration: MCPIntegration, name: str):
    """Call client_manager.get_client(name) and await if it returns a coroutine.

    Some implementations expose `get_client` as a coroutine (tests may use
    AsyncMock), while others provide a synchronous method. This helper
    abstracts that difference so callers can `await _maybe_await_get_client(..)`.
    """
    try:
        res = mcp_integration.client_manager.get_client(name)
    except Exception as e:
        logger.debug(f"Failed to get client {name}: {e}")
        return None
    import inspect
    if inspect.isawaitable(res):
        try:
            return await res
        except Exception as e:
            logger.debug(f"Failed to await client {name}: {e}")
            return None
    return res


async def _mcp_list_servers(mcp_service: MCPService, args: Any) -> None:
    """List configured external MCP servers using MCPService."""
    try:
        servers = await mcp_service.list_servers()

        if args.out_format == "json":
            print(json.dumps(servers, indent=2, ensure_ascii=False))
        else:
            # Table format
            if not servers:
                print("No external MCP servers configured.")
                return

            rows = []
            for server in servers:
                status = server.get("status", "unknown")

                if _supports_color():
                    if status == "connected":
                        status = _colorize("Connected", "32")  # green
                    elif status == "disconnected":
                        status = _colorize("Disconnected", "31")  # red
                    else:
                        status = _colorize("Disabled", "90")  # gray

                rows.append((
                    server["name"],
                    server.get("address", ""),
                    status,
                    server.get("description", "")
                ))

            headers = ["NAME", "ADDRESS", "STATUS", "DESCRIPTION"]
            if tabulate:
                print(tabulate(rows, headers=headers, tablefmt="github"))
            else:
                # Simple fallback
                if rows:
                    name_w = max(len(str(r[0])) for r in rows)
                    addr_w = max(len(str(r[1])) for r in rows)
                    status_w = max(len(str(r[2])) for r in rows)
                    desc_w = max(len(str(r[3])) for r in rows)
                else:
                    name_w = addr_w = status_w = desc_w = 10
                hdr = f"{'NAME'.ljust(name_w)}  {'ADDRESS'.ljust(addr_w)}  {'STATUS'.ljust(status_w)}  {'DESCRIPTION'.ljust(desc_w)}"
                print(hdr)
                print("-" * len(hdr))
                for n, a, s, d in rows:
                    print(f"{str(n).ljust(name_w)}  {str(a).ljust(addr_w)}  {str(s).ljust(status_w)}  {str(d).ljust(desc_w)}")

    except Exception as e:
        logger.exception("Failed to list MCP servers: %s", e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_connect_server(mcp_service: MCPService, server_name: str, args: Any) -> None:
    """Connect to an external MCP server using MCPService."""
    try:
        result = await mcp_service.connect_server(server_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to connect to server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_disconnect_server(mcp_service: MCPService, server_name: str, args: Any) -> None:
    """Disconnect from an external MCP server using MCPService."""
    try:
        result = await mcp_service.disconnect_server(server_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to disconnect from server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_status_servers(mcp_service: MCPService, server_name: str | None, args: Any) -> None:
    """Show status of external MCP servers using MCPService."""
    try:
        if server_name:
            status_info = await mcp_service.get_server_status(server_name)
            print(json.dumps(status_info, indent=2, ensure_ascii=False))
        else:
            # Status for all servers - just list them
            await _mcp_list_servers(mcp_service, args)
    except Exception as e:
        logger.exception("Failed to get server status: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_test_server(mcp_service: MCPService, server_name: str, args: Any) -> None:
    """Test connectivity and basic functionality of an external MCP server using MCPService."""
    try:
        result = await mcp_service.test_server(server_name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to test server: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_tool_management(tool_service: ToolService, server_name: str, args: Any) -> None:
    """Manage tools for a specific MCP server (list, allow, block) using ToolService."""
    # Tool action is in the 'key' argument
    tool_action = getattr(args, 'key', None)
    if not tool_action:
        print(json.dumps({"error": "Tool action required: list, allow, or block"}, ensure_ascii=False))
        return

    if tool_action == "list":
        await _list_server_tools_via_service(tool_service, server_name, args)
    elif tool_action == "allow":
        tool_name = getattr(args, 'value', None)
        if not tool_name:
            print(json.dumps({"error": "Tool name required for allow action"}, ensure_ascii=False))
            return
        await _allow_server_tool(tool_service, server_name, tool_name)
    elif tool_action == "block":
        tool_name = getattr(args, 'value', None)
        if not tool_name:
            print(json.dumps({"error": "Tool name required for block action"}, ensure_ascii=False))
            return
        await _block_server_tool(tool_service, server_name, tool_name)
    else:
        print(json.dumps({"error": f"Unknown tool action: {tool_action}. Use list, allow, or block"}, ensure_ascii=False))


async def _list_server_tools_via_service(tool_service: ToolService, server_name: str, args: Any) -> None:
    """List all available tools for a server using ToolService."""
    try:
        result = await tool_service.list_tools(server_name, include_filtering=True)

        # Handle output format
        if getattr(args, 'out_format', 'json') == "table":
            # Table format output
            print(f"\nServer: {server_name}")
            print("=" * (len(server_name) + 8))

            available_tools = result.get("tools", [])
            if not available_tools:
                print("No tools available")
            else:
                filtering = result.get("filtering", {})
                blocked = set(filtering.get("blocked_tools") or [])
                allowed = set(filtering.get("allowed_tools") or [])

                print(f"\nAvailable Tools ({len(available_tools)}):")
                print("-" * 30)
                for tool in available_tools:
                    status = ""
                    if tool in blocked:
                        status = " [BLOCKED]"
                    elif allowed and tool not in allowed:
                        status = " [NOT ALLOWED]"
                    print(f"  {tool}{status}")

                effective = result.get("effective_tools", available_tools)
                print(f"\nEffective Tools ({len(effective)}):")
                print("-" * 30)
                for tool in effective:
                    print(f"  {tool}")

                if blocked or allowed:
                    print("\nFiltering Configuration:")
                    print("-" * 30)
                    if allowed:
                        print(f"  Allowed: {', '.join(sorted(allowed))}")
                    if blocked:
                        print(f"  Blocked: {', '.join(sorted(blocked))}")
        else:
            # JSON format output
            print(json.dumps(result, indent=2, ensure_ascii=False))

    except Exception as e:
        logger.exception("Failed to list tools: server=%s, error=%s", server_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _allow_server_tool(tool_service: ToolService, server_name: str, tool_name: str) -> None:
    """Allow a tool for a server using ToolService."""
    try:
        result = await tool_service.allow_tool(server_name, tool_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to allow tool: server=%s, tool=%s, error=%s", server_name, tool_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _block_server_tool(tool_service: ToolService, server_name: str, tool_name: str) -> None:
    """Block a tool for a server using ToolService."""
    try:
        result = await tool_service.block_tool(server_name, tool_name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.exception("Failed to block tool: server=%s, tool=%s, error=%s", server_name, tool_name, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


async def _mcp_server_mode(config: Any, action: str, args: Any) -> None:
    """Manage MCP server mode."""
    mcp_integration = None

    try:
        # Initialize MCPIntegration for actions that need it
        if action in ("tools",):
            from .mcp.integration import MCPIntegration
            mcp_integration = MCPIntegration(config=config)
            try:
                await mcp_integration.initialize(config)
            except Exception as e:
                logger.warning(f"MCP integration initialization failed: {e}", exc_info=True)

        if action == "status":
            # Show MCP server configuration and status
            server_config = _get_server_mode_config(config)
            if not server_config:
                print(json.dumps({"error": "MCP server mode not configured"}, ensure_ascii=False))
                return

            status_data = {
                "enabled": server_config.enabled,
                "endpoint": server_config.endpoint,
                "expose_plugins": server_config.expose_plugins,
                "authentication": {
                    "required": server_config.authentication.required,
                    "methods": server_config.authentication.methods
                },
                "rate_limit": {
                    "enabled": server_config.rate_limit.enabled,
                    "requests_per_minute": server_config.rate_limit.requests_per_minute,
                    "requests_per_hour": server_config.rate_limit.requests_per_hour,
                    "burst_size": server_config.rate_limit.burst_size
                },
                "session_ttl": server_config.session_ttl
            }

            if args.out_format == "json":
                print(json.dumps(status_data, indent=2, ensure_ascii=False))
            else:
                print("MCP Server Mode Configuration")
                print("=" * 40)
                print(f"Enabled: {status_data['enabled']}")
                print(f"Endpoint: {status_data['endpoint']}")
                print(f"Exposed Plugins: {', '.join(status_data['expose_plugins'])}")
                print("\nAuthentication:")
                print(f"  Required: {status_data['authentication']['required']}")
                print(f"  Methods: {', '.join(status_data['authentication']['methods'])}")
                print("\nRate Limiting:")
                print(f"  Enabled: {status_data['rate_limit']['enabled']}")
                print(f"  Requests/Minute: {status_data['rate_limit']['requests_per_minute']}")
                print(f"  Requests/Hour: {status_data['rate_limit']['requests_per_hour']}")
                print(f"  Burst Size: {status_data['rate_limit']['burst_size']}")
                print(f"\nSession TTL: {status_data['session_ttl']} seconds")

        elif action == "tools":
            # List tools exposed by MCP server mode
            server_config = _get_server_mode_config(config)
            if not server_config or not server_config.enabled:
                print(json.dumps({"error": "MCP server mode is not enabled"}, ensure_ascii=False))
                return

            # Get list of tools that would be exposed
            tools = []

            # Use the plugin_registry from MCPIntegration
            plugin_registry = mcp_integration.plugin_registry if mcp_integration else None
            if not plugin_registry:
                print(json.dumps({"error": "Plugin registry not available"}, ensure_ascii=False))
                return

            for plugin_name in server_config.expose_plugins:
                if plugin_name == "*":
                    # Expose all plugins - iterate over plugin_servers dict
                    for name, adapter in plugin_registry.plugin_servers.items():
                        try:
                            if hasattr(adapter, 'list_tools'):
                                plugin_tools = await adapter.list_tools()
                                # Handle both direct list and ListToolsResult object
                                tool_list = plugin_tools.tools if hasattr(plugin_tools, 'tools') else plugin_tools
                                for tool in tool_list:
                                    tools.append({
                                        "plugin": name,
                                        "name": tool.name,
                                        "description": tool.description
                                    })
                        except Exception as e:
                            logger.warning(f"Error listing tools for plugin {name}: {e}", exc_info=True)
                            continue
                else:
                    # Expose specific plugin
                    try:
                        adapter = plugin_registry.plugin_servers.get(plugin_name)
                        if adapter and hasattr(adapter, 'list_tools'):
                            plugin_tools = await adapter.list_tools()
                            # Handle both direct list and ListToolsResult object
                            tool_list = plugin_tools.tools if hasattr(plugin_tools, 'tools') else plugin_tools
                            for tool in tool_list:
                                tools.append({
                                    "plugin": plugin_name,
                                    "name": tool.name,
                                    "description": tool.description
                                })
                    except Exception as e:
                        logger.warning(f"Error listing tools for plugin {plugin_name}: {e}", exc_info=True)
                        continue

            # Clean up integration
            if mcp_integration:
                try:
                    await mcp_integration.shutdown()
                except Exception as e:
                    logger.debug(f"Error shutting down MCPIntegration: {e}")

            if args.out_format == "json":
                print(json.dumps({"tools": tools}, indent=2, ensure_ascii=False))
            else:
                # Format as nice table like agent-cli plugins
                rows = []
                for tool in tools:
                    full_name = f"{tool['plugin']}.{tool['name']}"
                    # Limit description to 120 chars
                    desc = tool['description']
                    if len(desc) > 120:
                        desc = desc[:117] + "..."
                    rows.append((full_name, desc))

                headers = ["TOOL NAME", "DESCRIPTION"]

                if tabulate:
                    print(f"\nExposed Tools ({len(tools)}):")
                    print(tabulate(rows, headers=headers, tablefmt="github"))
                else:
                    # Simple fallback without tabulate
                    if rows:
                        name_w = max(len(r[0]) for r in rows)
                    else:
                        name_w = 20

                    print(f"\nExposed Tools ({len(tools)}):")
                    hdr = f"{'TOOL NAME'.ljust(name_w)}  DESCRIPTION"
                    print(hdr)
                    print("-" * len(hdr))
                    for name, desc in rows:
                        # Wrap long descriptions
                        if len(desc) > 80:
                            # Print first line with name
                            print(f"{name.ljust(name_w)}  {desc[:80]}")
                            # Print continuation lines
                            remaining = desc[80:]
                            while remaining:
                                print(f"{' ' * (name_w + 2)}{remaining[:80]}")
                                remaining = remaining[80:]
                        else:
                            print(f"{name.ljust(name_w)}  {desc}")

        elif action == "sessions":
            # List active MCP sessions
            server_config = _get_server_mode_config(config)
            if not server_config or not server_config.enabled:
                print(json.dumps({"error": "MCP server mode is not enabled"}, ensure_ascii=False))
                return

            # Query the running server for session information
            try:
                import httpx
                # Build the full URL - endpoint is just the path
                endpoint_path = server_config.endpoint.rstrip('/')
                # Default to localhost:8000 for server info endpoint
                base_url = "http://127.0.0.1:8000"
                url = f"{base_url}{endpoint_path}/server-info"

                # Get CLI request timeout from config (default 5.0 seconds)
                timeout = config.network.cli_request_timeout if config and config.network else 5.0
                
                async with httpx.AsyncClient(timeout=timeout) as client:
                    try:
                        response = await client.get(url)
                        if response.status_code == 200:
                            data = response.json()
                            sessions = data.get("sessions", {})

                            if args.out_format == "json":
                                print(json.dumps(sessions, indent=2, ensure_ascii=False))
                            else:
                                # Format as table
                                active = sessions.get("active", 0)
                                total = sessions.get("total", 0)
                                print("\nMCP Server Sessions:")
                                print(f"  Active: {active}")
                                print(f"  Total:  {total}")

                                # If there are details about individual sessions
                                if "details" in sessions and sessions["details"]:
                                    print("\n  Session Details:")
                                    for session_id, info in sessions["details"].items():
                                        print(f"    {session_id[:12]}... - {info}")
                        else:
                            print(json.dumps({
                                "error": f"Server returned status {response.status_code}",
                                "message": "MCP server might not be running"
                            }, indent=2, ensure_ascii=False))
                    except httpx.ConnectError:
                        print(json.dumps({
                            "error": "Cannot connect to MCP server",
                            "message": f"Server not running at {base_url}",
                            "info": "Use 'agent-cli mcp server start' to start the server"
                        }, indent=2, ensure_ascii=False))
            except ImportError:
                print(json.dumps({
                    "error": "httpx library not installed",
                    "message": "Install with: pip install httpx"
                }, indent=2, ensure_ascii=False))

        elif action == "config":
            # Show full MCP server configuration
            server_config = _get_server_mode_config(config)
            if not server_config:
                print(json.dumps({"error": "MCP server mode not configured"}, ensure_ascii=False))
                return

            config_dict = {
                "enabled": server_config.enabled,
                "endpoint": server_config.endpoint,
                "expose_plugins": server_config.expose_plugins,
                "authentication": {
                    "required": server_config.authentication.required,
                    "methods": server_config.authentication.methods
                },
                "rate_limit": {
                    "enabled": server_config.rate_limit.enabled,
                    "requests_per_minute": server_config.rate_limit.requests_per_minute,
                    "requests_per_hour": server_config.rate_limit.requests_per_hour,
                    "burst_size": server_config.rate_limit.burst_size
                },
                "session_ttl": server_config.session_ttl
            }
            print(json.dumps(config_dict, indent=2, ensure_ascii=False))

        else:
            print(json.dumps({"error": f"Unknown server action: {action}"}, ensure_ascii=False))

    except Exception as e:
        logger.exception("Failed to manage MCP server mode: action=%s, error=%s", action, e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
    finally:
        # Clean up integration if it was created
        if mcp_integration:
            try:
                await mcp_integration.shutdown()
            except Exception as e:
                logger.debug(f"Error shutting down MCPIntegration in finally: {e}")


def main() -> None:
    global logger
    # Backward-compatible: allow calling `agent-cli <task>` without an explicit subcommand.
    # If the first non-option arg isn't a known subcommand, inject an implicit 'run' subcommand.
    # Use a two-stage parse: first extract global options from anywhere using parse_known_args,
    # then parse the remaining args (subcommand + subargs). This avoids confusing option values
    # with subcommands when we need to insert an implicit 'run'.
    prelim = argparse.ArgumentParser(add_help=False)
    prelim.add_argument("--config", dest="config", default=str(Path("config/config.yaml")))
    prelim.add_argument("-v", "--verbose", dest="verbose", action="store_true")
    # color can be set to auto/always/never/ansi/html/text
    prelim.add_argument("--color", dest="color", choices=["auto", "always", "never", "ansi", "html", "text"], default="always")
    prelim.add_argument("--no-color", dest="no_color", action="store_true")
    prelim.add_argument("--show-mcp", dest="show_mcp", action="store_true")
    prelim.add_argument("--no-status", dest="no_status", action="store_true")
    prelim.add_argument("--raw", dest="raw", action="store_true")
    orig_args = sys.argv[1:]
    ns, rest = prelim.parse_known_args(orig_args)

    # decide color mode early so helpers behave predictably
    if getattr(ns, "no_color", False):
        set_color_mode("never")
    else:
        set_color_mode(getattr(ns, "color", "auto"))

    # Initialize colorama on interactive TTYs so ANSI renders on Windows
    try:
        mode = "never" if getattr(ns, "no_color", False) else getattr(ns, "color", "auto")
        if mode != "never" and sys.stdout.isatty():
            import colorama
            colorama.init()
    except Exception:
        pass

    # If the first token of the remaining args isn't a known subcommand, insert implicit 'run'
    known = ("plugins", "mcp", "hooks", "run", "status", "users", "-h", "--help")
    if rest:
        if not rest[0].startswith("-") and rest[0] not in known:
            rest.insert(0, "run")
    else:
        # no remaining tokens: nothing to parse further
        rest = []

    # Reconstruct final argv for full parsing: prepend any global options we care about
    final_args = []
    if getattr(ns, "config", None):
        final_args.extend(["--config", ns.config])
    if getattr(ns, "verbose", False):
        final_args.append("--verbose")
    if getattr(ns, "no_color", False):
        final_args.append("--no-color")
    elif getattr(ns, "color", "auto") != "auto":
        final_args.extend(["--color", ns.color])
    # Forward streaming/raw flags parsed in the preliminary stage so the
    # final parser receives the same intent (these flags may have been
    # placed anywhere on the command line by the user).
    if getattr(ns, "show_mcp", False):
        final_args.append("--show-mcp")
    if getattr(ns, "no_status", False):
        final_args.append("--no-status")
    if getattr(ns, "raw", False):
        final_args.append("--raw")
    # append the remaining tokens (subcommand + subargs)
    argv = [sys.argv[0]] + final_args + rest

    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("--config", dest="config", default=str(Path("config/config.yaml")), help="Path to config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print progress messages")
    parser.add_argument("--color", dest="color", choices=["auto", "always", "never", "ansi", "html", "text"], default="always",
                        help="Output format: auto=ANSI if TTY, always/ansi=ANSI colors, html=HTML, never/text=plain text")
    parser.add_argument("--no-color", dest="no_color", action="store_true", help="Disable color output (alias for --color never)")
    parser.add_argument("--show-mcp", dest="show_mcp", action="store_true", help="Show MCP call/result details (for debugging)")
    parser.add_argument("--no-status", dest="no_status", action="store_true", help="Hide status messages during execution")
    parser.add_argument("--raw", dest="raw", action="store_true", help="Output raw JSON result instead of pretty printing")
    subparsers = parser.add_subparsers(dest="subcommand")

    # run subcommand (default behavior)
    run_parser = subparsers.add_parser("run", help="Run an agent task (default)")
    run_parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")
    run_parser.add_argument("--images", "--attach", dest="images", nargs="+", metavar="PATH", help="Path(s) to image file(s) to attach to the task")
    run_parser.add_argument("--audio", dest="audio", nargs="+", metavar="PATH", help="Path(s) to audio file(s) to attach to the task (mp3, wav, ogg, etc.)")
    run_parser.add_argument("--text", "--files", dest="text_files", nargs="+", metavar="PATH", help="Path(s) to text file(s) to attach to the task (txt, md, py, json, etc.)")
    run_parser.add_argument("--agent", dest="agent_override", help="Override the default agent (use agent name from config)")
    run_parser.add_argument("--llm", dest="llm_profile_override", help="Override the LLM profile (use profile name from llm.yaml)")
    run_parser.add_argument("--session", dest="session_id", help="Continue an existing session by ID")
    run_parser.add_argument("--session-user", dest="session_user", default="cli_user", help="User ID for session storage (default: cli_user)")
    run_parser.add_argument("--list-sessions", dest="list_sessions", action="store_true", help="List all sessions for the current user")
    run_parser.add_argument("--session-title", dest="session_title", help="Title for the new session (auto-generated from task if not provided)")



    # plugins subcommand
    plugins_parser = subparsers.add_parser("plugins", help="Manage plugins")
    plugins_parser.add_argument("action", choices=["list", "info", "enable", "disable", "search", "status"], nargs="?", default="list", help="Action to perform on plugins")
    plugins_parser.add_argument("name", nargs="?", help="Plugin name for the 'info', 'enable', 'disable' actions or search term for 'search'")
    plugins_parser.add_argument("--yes", dest="yes", action="store_true", help="Assume yes for confirmations")
    plugins_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Don't persist changes; show preview")
    plugins_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for plugin listing")
    plugins_parser.add_argument("--show-metadata", dest="show_metadata", action="store_true", help="Also display plugin._plugin_metadata in listing (JSON output only)")
    plugins_parser.add_argument("--raw", dest="raw", action="store_true", help="Show raw factory information for 'info' action")

    # mcp subcommand for external server management (use subparsers so each
    # action can provide its own help output). We keep argument names that
    # the existing handler expects (`server`, `key`, `value`) for
    # backwards-compatibility with the rest of the code.
    mcp_parser = subparsers.add_parser("mcp", help="Manage external MCP servers")
    # Global options for mcp
    mcp_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for server listing")
    mcp_parser.add_argument("--timeout", dest="timeout", type=int, default=30, help="Timeout in seconds for connection operations")
    mcp_parser.add_argument("--no-probe", dest="no_probe", action="store_true", help="When listing features, don't probe the live server for reported capabilities; only show configured values")

    # Per-action subparsers
    # Provide a description so `mcp --help` shows a helpful line expected by tests.
    mcp_subparsers = mcp_parser.add_subparsers(dest="action", description="Action to perform on external MCP servers")

    # Helper to add mcp-level options to individual action subparsers so
    # users may place them after the action (e.g. `mcp list --format json`).
    def _add_mcp_common_opts(p):
        try:
            p.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format for server listing")
        except Exception as e:
            logger.warning(f"Failed to add --format argument to parser: {e}", exc_info=True)
        try:
            p.add_argument("--timeout", dest="timeout", type=int, default=30, help="Timeout in seconds for connection operations")
        except Exception as e:
            logger.warning(f"Failed to add --timeout argument to parser: {e}", exc_info=True)
        try:
            p.add_argument("--no-probe", dest="no_probe", action="store_true", help="When listing features, don't probe the live server for reported capabilities; only show configured values")
        except Exception as e:
            logger.warning(f"Failed to add --no-probe argument to parser: {e}", exc_info=True)

    # list
    list_p = mcp_subparsers.add_parser("list", help="List configured external MCP servers")
    _add_mcp_common_opts(list_p)

    # connect / disconnect
    connect_p = mcp_subparsers.add_parser("connect", help="Connect to an external MCP server")
    _add_mcp_common_opts(connect_p)
    connect_p.add_argument("server", nargs="?", help="Server name to connect")
    disconnect_p = mcp_subparsers.add_parser("disconnect", help="Disconnect from an external MCP server")
    _add_mcp_common_opts(disconnect_p)
    disconnect_p.add_argument("server", nargs="?", help="Server name to disconnect")

    # status
    status_p = mcp_subparsers.add_parser("status", help="Show status for a server or all servers")
    _add_mcp_common_opts(status_p)
    status_p.add_argument("server", nargs="?", help="Optional server name to show status for")

    # test
    test_p = mcp_subparsers.add_parser("test", help="Test connectivity and basic functionality of an external MCP server")
    _add_mcp_common_opts(test_p)
    test_p.add_argument("server", nargs="?", help="Server name for test action")

    # enable / disable (persisted to config)
    enable_p = mcp_subparsers.add_parser("enable", help="Enable a configured external MCP server")
    _add_mcp_common_opts(enable_p)
    enable_p.add_argument("server", nargs="?", help="Server name to enable")
    disable_p = mcp_subparsers.add_parser("disable", help="Disable a configured external MCP server")
    _add_mcp_common_opts(disable_p)
    disable_p.add_argument("server", nargs="?", help="Server name to disable")

    # feature (keeps key/value semantics)
    feature_p = mcp_subparsers.add_parser("feature", help="Manage MCP feature flags")
    _add_mcp_common_opts(feature_p)
    feature_p.add_argument("server", nargs="?", help="Server name for feature actions")
    feature_p.add_argument("key", nargs="?", help="Feature subcommand or feature name (for feature set)")
    feature_p.add_argument("value", nargs="?", help="Feature value (on|off) for feature set)")

    # tool subcommand: provide natural help for tool usage
    tool_p = mcp_subparsers.add_parser("tool", help="Manage individual tools on an MCP server")
    _add_mcp_common_opts(tool_p)
    tool_p.add_argument("server", nargs="?", help="Server name for tool actions")
    tool_p.add_argument("key", nargs="?", choices=["list", "allow", "block"], help="Tool action: list, allow, or block")
    tool_p.add_argument("value", nargs="?", help="Tool name for allow/block actions")
    # enable/disable always persist; no interactive prompt or dry-run

    # MCP server mode subcommands
    server_p = mcp_subparsers.add_parser("server", help="Manage MCP server mode")
    _add_mcp_common_opts(server_p)
    server_p.add_argument("server_action", nargs="?", choices=["status", "tools", "config", "sessions"], default="status",
                         help="Server action: status (default), tools (list exposed tools), config (show configuration), sessions (list active sessions)")
    server_p.add_argument("--session-id", dest="session_id", help="Session ID to query (for 'sessions' action)")

    # hooks subcommand for hook introspection
    hooks_parser = subparsers.add_parser("hooks", help="Hook introspection and debugging")
    hooks_parser.add_argument("action", choices=["list", "inspect", "stats", "clear-stats"], nargs="?", default="list", help="Action to perform")
    hooks_parser.add_argument("name", nargs="?", help="Hook name for 'inspect' action")
    hooks_parser.add_argument("--type", dest="hook_type", help="Filter by hook type (e.g., PRE_LLM_CALL, POST_LLM_CALL)")
    hooks_parser.add_argument("--format", dest="out_format", choices=["json", "table"], default="table", help="Output format")

    # users subcommand for user management
    users_parser = subparsers.add_parser(
        "users",
        help="Manage users (admin)",
        description="User management commands. Examples:\n"
                    "  agent-cli users list\n"
                    "  agent-cli users info admin\n"
                    "  agent-cli users update admin --activate\n"
                    "  agent-cli users create newuser user@example.com --password secret",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    users_parser.add_argument("action", choices=["list", "create", "delete", "update", "info", "generate-api-key", "revoke-api-key"], nargs="?", default="list", help="Action to perform (default: list)")
    users_parser.add_argument("username", nargs="?", help="Target username to operate on")
    users_parser.add_argument("email", nargs="?", help="Email address (required for 'create' action)")
    users_parser.add_argument("--password", "-p", dest="password", help="User password (for create/update)")
    users_parser.add_argument("--name", "-n", dest="full_name", help="User's full display name (e.g., 'John Doe')")
    users_parser.add_argument("--role", "-r", dest="role", choices=["user", "admin", "guest"], help="User role (for create/update)")
    users_parser.add_argument("--admin", dest="admin", action="store_true", help="Make user an admin (shortcut for --role admin)")
    users_parser.add_argument("--inactive", dest="inactive", action="store_true", help="Create user as inactive (for 'create' action)")
    users_parser.add_argument("--activate", dest="activate", action="store_true", help="Activate user (for 'update' action)")
    users_parser.add_argument("--deactivate", dest="deactivate", action="store_true", help="Deactivate user (for 'update' action)")
    users_parser.add_argument("--force", "-f", dest="force", action="store_true", help="Skip confirmation prompts")
    users_parser.add_argument("--limit", dest="limit", type=int, default=100, help="Maximum number of users to show (for 'list' action)")
    users_parser.add_argument("--skip", dest="skip", type=int, default=0, help="Number of users to skip (for 'list' action)")

    args = parser.parse_args(argv[1:])

    # If no subcommand was provided, show help and exit instead of proceeding
    if not getattr(args, "subcommand", None):
        parser.print_help()
        return

    def vprint(msg: str) -> None:
        if args.verbose:
            print(msg, flush=True)

    vprint("[cli] verbose mode on")
    vprint(f"[cli] loading config: {args.config}")
    config = load_settings(args.config)
    # If user requested plugin listing, handle and exit early (no heavy bootstrap)
    if args.subcommand == "plugins":
        # Use the configured plugin_dirs from the loaded settings. The
        # `load_settings()` call resolves relative paths against the
        # config file directory, so we can trust these paths as provided by
        # the user. If no plugin dirs are configured, pass None to
        # `discover_all_plugins()` to discover only entrypoint plugins.
        plugins_cfg = _get_plugins_config(config)
        dirs = [Path(p) for p in (plugins_cfg.plugin_dirs or []) if p] if plugins_cfg else []
        plugins = discover_all_plugins(dirs if dirs else None)

        def to_list():
            """Build a list of plugins with their instances grouped by type."""
            out = []
            # Build a mapping of plugin_type -> list of instances
            type_to_instances = {}

            if plugins_cfg:
                for instance_name, mcp_config in plugins_cfg.servers.items():
                    plugin_type = mcp_config.type
                    if plugin_type not in type_to_instances:
                        type_to_instances[plugin_type] = []
                    type_to_instances[plugin_type].append({
                        "instance_name": instance_name,
                        "enabled": mcp_config.enabled,
                        "description": mcp_config.description or "",
                    })

            # Now build the output list with plugin types and their instances
            for plugin_type, factory in plugins.items():
                meta = getattr(factory, "_plugin_metadata", None) or {}
                instances = type_to_instances.get(plugin_type, [])

                # Check if any instance of this type is enabled
                any_enabled = any(inst["enabled"] for inst in instances)

                plugin_entry = {
                    "name": plugin_type,
                    "description": meta.get("description"),
                    "version": meta.get("version"),
                    "enabled": any_enabled,
                    "instances": instances if len(instances) > 1 else [],  # Only show instances if multiple exist
                }
                out.append(plugin_entry)

            return out

        # info action: print metadata for a specific plugin
        if getattr(args, "action", None) == "info":
            target = getattr(args, "name", None)
            if not target:
                print(json.dumps({"error": "missing plugin name"}, ensure_ascii=False))
                return
            factory = plugins.get(target)
            if not factory:
                print(json.dumps({"error": "plugin not found", "name": target}, ensure_ascii=False))
                return
            meta = getattr(factory, "_plugin_metadata", None) or {}
            # raw output: include factory repr and module path
            # Accept either the parsed flag or fallback to detecting '--raw'
            # in sys.argv to be resilient to argument ordering and parser quirks.
            raw_flag = getattr(args, "raw", False) or ("--raw" in sys.argv)
            if raw_flag:
                # Ensure we always include these keys so downstream callers/tests
                # can rely on stable JSON shape. Use safe fallbacks if repr()
                # or attribute access fails.
                try:
                    fr = repr(factory)
                except Exception:
                    fr = None
                fm = getattr(factory, "__module__", None)
                factory_info = {
                    "factory_repr": fr,
                    "factory_module": fm,
                }
                out = {"name": target, "metadata": meta, **factory_info}
                plugins_cfg = _get_plugins_config(config)
                enabled_servers = [k for k, v in plugins_cfg.servers.items() if v.enabled] if plugins_cfg else []
                out["enabled"] = target in enabled_servers
                if args.out_format == "table":
                    # Print header and key/value lines
                    print(f"NAME: {target}")
                    for k, v in out.items():
                        if k == "name":
                            continue
                        print(f"{k.upper()}: {v}")
                    return
                print(json.dumps(out, indent=2, ensure_ascii=False))
                return

            # human table optionally
            if args.out_format == "table":
                # Print the plugin name header followed by metadata key: value lines
                print(f"NAME: {target}")
                # Always print DESCRIPTION and VERSION lines (may be blank) to keep output stable
                print(f"DESCRIPTION: {meta.get('description', '')}")
                print(f"VERSION: {meta.get('version', '')}")
                # show enabled status for this plugin
                plugins_cfg = _get_plugins_config(config)
                enabled_servers = [k for k, v in plugins_cfg.servers.items() if v.enabled] if plugins_cfg else []
                enabled_flag = target in enabled_servers
                enabled_text = "YES" if enabled_flag else "NO"
                display_enabled = enabled_text
                if _supports_color():
                    if enabled_flag:
                        display_enabled = _colorize(enabled_text, "32")
                    else:
                        display_enabled = _colorize(enabled_text, "31")
                print(f"ENABLED: {display_enabled}")
                return
            print(json.dumps({"name": target, "metadata": meta}, indent=2, ensure_ascii=False))
            return

        # REMOVED: enable/disable actions (Task #9265 - Epic 0044)
        # Config mutation removed - users should edit config files directly
        if getattr(args, "action", None) in ("enable", "disable"):
            print(json.dumps({
                "error": "enable/disable commands removed",
                "message": "Please edit config files directly (config/plugins.yaml)",
                "info": "Set 'enabled: true/false' for the specific plugin server in plugins.yaml"
            }, ensure_ascii=False))
            return

        # search action: filter plugins by name or description
        if getattr(args, "action", None) == "search":
            term = (getattr(args, "name", None) or "").lower()
            listing = to_list()
            filtered = [p for p in listing if term in (p["name"] or "").lower() or term in (p.get("description") or "").lower()]
            print(json.dumps(filtered, indent=2, ensure_ascii=False))
            return

        # status action: show discovered plugins and whether they're enabled in config
        if getattr(args, "action", None) == "status":
            # use the properly loaded config (with includes processed) instead of reading file directly
            listing = to_list()
            print(json.dumps(listing, indent=2, ensure_ascii=False))
            return

        # list action: either json or simple table
        listing = to_list()
        # If user requested metadata in the listing and JSON format, attach it
        if getattr(args, "show_metadata", False) and args.out_format == "json":
            for item in listing:
                factory = plugins.get(item.get("name"))
                item["metadata"] = getattr(factory, "_plugin_metadata", None) or {}

        # Metadata should have been attached by discover_all_plugins() when
        # filesystem plugin dirs were provided. If any metadata is still
        # missing, leave it blank rather than attempting to read repository
        # paths — callers should configure plugin_dirs in `mcp.yaml` if
        # they expect filesystem plugin metadata to be used.
        if args.out_format == "table":
            # nice table layout using tabulate if available
            rows: List[Tuple[str, str, str, str]] = []
            for p in listing:
                enabled_flag = bool(p.get("enabled"))
                enabled_text = "YES" if enabled_flag else "NO"
                display_enabled = enabled_text
                if _supports_color():
                    if enabled_flag:
                        display_enabled = _colorize(enabled_text, "32")
                    else:
                        display_enabled = _colorize(enabled_text, "31")

                # Add the main plugin type row with truncated description
                desc = p.get("description") or ""
                if len(desc) > 80:
                    desc = desc[:77] + "..."
                rows.append((p.get("name") or "", display_enabled, desc, p.get("version") or ""))

                # Add instance rows if multiple instances exist
                instances = p.get("instances", [])
                if instances:
                    for inst in instances:
                        inst_enabled = inst.get("enabled", False)
                        inst_enabled_text = "YES" if inst_enabled else "NO"
                        inst_display_enabled = inst_enabled_text
                        if _supports_color():
                            if inst_enabled:
                                inst_display_enabled = _colorize(inst_enabled_text, "32")
                            else:
                                inst_display_enabled = _colorize(inst_enabled_text, "31")

                        # Indent instance name with tree characters
                        inst_name = f"  ├─ {inst.get('instance_name', '')}"
                        inst_desc = inst.get("description", "")
                        if len(inst_desc) > 80:
                            inst_desc = inst_desc[:77] + "..."
                        rows.append((inst_name, inst_display_enabled, inst_desc, ""))

            headers = ["NAME", "ENABLED", "DESCRIPTION", "VERSION"]
            if tabulate:
                print(tabulate(rows, headers=headers, tablefmt="github"))
            else:
                # simple fallback
                if rows:
                    name_w = max(len(r[0]) for r in rows)
                    enabled_w = max(len(r[1]) for r in rows)
                    desc_w = max(len(r[2]) for r in rows)
                else:
                    name_w = enabled_w = desc_w = 10
                hdr = f"{'NAME'.ljust(name_w)}  {'ENABLED'.ljust(enabled_w)}  {'DESCRIPTION'.ljust(desc_w)}  VERSION"
                print(hdr)
                print("-" * len(hdr))
                for n, e, d, v in rows:
                    print(f"{n.ljust(name_w)}  {e.ljust(enabled_w)}  {d.ljust(desc_w)}  {v}")
            return

        print(json.dumps(listing, indent=2, ensure_ascii=False))
        return

    # Handle hooks introspection subcommand
    if args.subcommand == "hooks":
        handle_hooks_command(args)
        return

    # Handle MCP external server management subcommand
    if args.subcommand == "mcp":
        # Use config loaded via settings.py - no direct YAML access
        # All config mutations removed - users should edit config files directly

        async def handle_mcp_command():
            # Use direct AgentConfig approach for consistency with main CLI bootstrapping
            mcp_integration = MCPIntegration(config=config)
            # Ensure MCPIntegration sets up external clients and plugins
            try:
                try:
                    await mcp_integration.initialize(config)
                except Exception as e:
                    # Non-fatal: continue without live clients if initialization fails
                    logger.warning(f"MCP integration initialization failed, continuing without live clients: {e}", exc_info=True)

                # Initialize services for clean separation of concerns
                mcp_service = MCPService(mcp_integration, config)
                tool_service = ToolService(mcp_integration, config)

                action = getattr(args, "action", "list")
                server_name = getattr(args, "server", None)
                if action == "list":
                    result = await _mcp_list_servers(mcp_service, args)
                    return
                elif action in ("enable", "disable"):
                    # Config mutation removed - edit config files directly
                    print(json.dumps({
                        "error": "enable/disable commands removed",
                        "message": "Please edit config files directly (config/mcp_servers.yaml)",
                        "info": "Set 'enabled: true/false' for the specific server in mcp_servers.yaml"
                    }, ensure_ascii=False))
                    return
                elif action == "feature":
                    # Config mutation removed - edit config files directly
                    sub = getattr(args, "key", None)
                    if not server_name:
                        print(json.dumps({"error": "server name required for feature action"}, ensure_ascii=False))
                        return

                    # LIST features: query server capabilities via client if connected
                    if sub == "list" or sub is None:
                        # Try client if available to get actual capabilities
                        # honor --no-probe: skip live query if requested
                        if getattr(args, "no_probe", False):
                            client = None
                        else:
                            client = await _maybe_await_get_client(mcp_integration, server_name)
                        capabilities = None
                        if client:
                            try:
                                # initialize may already have been called; server info stored in client
                                cap = getattr(client, 'server_capabilities', None)
                                if cap:
                                    capabilities = cap
                                else:
                                    # attempt to re-init
                                    try:
                                        await client.initialize()
                                        capabilities = getattr(client, 'server_capabilities', None)
                                    except Exception as e:
                                        logger.debug(f"Failed to reinitialize client for {server_name}: {e}")
                                        capabilities = None
                            except Exception as e:
                                logger.debug(f"Failed to get capabilities for {server_name}: {e}")
                                capabilities = None

                        # Get configured features from config object (read-only)
                        # Get MCP servers config
                        mcp_servers_cfg = config.external_servers
                        conf_features = {}
                        if mcp_servers_cfg and hasattr(mcp_servers_cfg, 'remote_servers'):
                            server_cfg = mcp_servers_cfg.remote_servers.get(server_name, {})
                            conf_features = server_cfg.get('features', {}) if isinstance(server_cfg, dict) else {}

                        out = {
                            'server': server_name,
                            'configured_features': conf_features,
                            'reported_capabilities': capabilities
                        }
                        print(json.dumps(out, indent=2, ensure_ascii=False))
                        result = None
                        return

                    # SET feature: Config mutation removed
                    print(json.dumps({
                        "error": "feature set command removed",
                        "message": "Please edit config files directly (config/mcp_servers.yaml)",
                        "info": "Update 'features' section for the specific server in mcp_servers.yaml"
                    }, ensure_ascii=False))
                    return
                elif action == "connect":
                    if not server_name:
                        print(json.dumps({"error": "server name required for connect action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_connect_server(mcp_service, server_name, args)
                    return
                elif action == "disconnect":
                    if not server_name:
                        print(json.dumps({"error": "server name required for disconnect action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_disconnect_server(mcp_service, server_name, args)
                    return
                elif action == "status":
                    result = await _mcp_status_servers(mcp_service, server_name, args)
                    return
                elif action == "test":
                    if not server_name:
                        print(json.dumps({"error": "server name required for test action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_test_server(mcp_service, server_name, args)
                    return
                elif action == "server":
                    # MCP server mode management
                    server_action = getattr(args, "server_action", "status")
                    result = await _mcp_server_mode(config, server_action, args)
                    return
                elif action == "tool":
                    if not server_name:
                        print(json.dumps({"error": "server name required for tool action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_tool_management(tool_service, server_name, args)
                    return
            finally:
                # Ensure we always attempt to shutdown the integration so any
                # created aiohttp client sessions are closed and we don't leak
                # resources when the CLI command exits.
                try:
                    logging.getLogger(__name__).debug("MCPIntegration: calling shutdown()")
                    await mcp_integration.shutdown()
                    logging.getLogger(__name__).debug("MCPIntegration: shutdown() completed")
                except Exception as e:
                    logging.getLogger(__name__).debug(f"Error shutting down MCPIntegration: {e}")
            # Return the captured result (if any) after shutdown completes.
            # Use locals().get to avoid UnboundLocalError when `result` was
            # never assigned due to early returns inside the try/finally.
            return locals().get('result', None)

        # Run the async MCP handler
        try:
            asyncio.run(handle_mcp_command())
        except Exception as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return

    # Handle users management subcommand
    if args.subcommand == "users":
        # Import typer-based user CLI using importlib to avoid package/module name conflict
        # (cli.py and cli/ directory both exist)
        import importlib.util

        # Dynamically import users module from cli_utils/ directory
        users_module_path = Path(__file__).parent / "cli_utils" / "users.py"
        spec = importlib.util.spec_from_file_location("agent_system.agent_cli.users", users_module_path)
        if spec and spec.loader:
            users_module = importlib.util.module_from_spec(spec)
            sys.modules["agent_system.agent_cli.users"] = users_module
            spec.loader.exec_module(users_module)
            users_app = users_module.app
        else:
            print("ERROR: Could not load users module", file=sys.stderr)
            return

        # Build arguments for typer command
        typer_args = [args.action] if args.action else []

        # Add positional arguments (username, email for create command)
        if args.username:
            typer_args.append(args.username)
        if hasattr(args, 'email') and args.email and args.action == 'create':
            typer_args.append(args.email)

        # Add optional arguments
        if hasattr(args, 'password') and args.password:
            typer_args.extend(['--password', args.password])
        if hasattr(args, 'full_name') and args.full_name:
            typer_args.extend(['--name', args.full_name])
        if hasattr(args, 'role') and args.role:
            typer_args.extend(['--role', args.role])
        if hasattr(args, 'admin') and args.admin:
            typer_args.append('--admin')
        if hasattr(args, 'inactive') and args.inactive:
            typer_args.append('--inactive')
        if hasattr(args, 'activate') and args.activate:
            typer_args.append('--activate')
        if hasattr(args, 'deactivate') and args.deactivate:
            typer_args.append('--deactivate')
        if hasattr(args, 'force') and args.force:
            typer_args.append('--force')
        # limit and skip are only for 'list' action
        if args.action == 'list':
            if hasattr(args, 'limit') and args.limit:
                typer_args.extend(['--limit', str(args.limit)])
            if hasattr(args, 'skip') and args.skip:
                typer_args.extend(['--skip', str(args.skip)])

        # Execute typer command
        try:
            users_app(typer_args, standalone_mode=False)
        except SystemExit:
            # Typer raises SystemExit, catch it to prevent full CLI exit
            pass
        return

    # Setup logging from config; file handler is created here. Console level is adjusted below.
    # Use a role-specific logfile so concurrent processes (cli vs api) don't
    # clobber the same file. If the configured file is `logs/agent.log` this
    # will create `logs/agent-cli.log` for the CLI.
    def _role_logfile(base: str, role: str) -> str:
        try:
            p = Path(base)
            stem = p.stem or "agent"
            # preserve all suffixes (e.g. .log)
            suffix = "".join(p.suffixes) or ".log"
            return str(p.with_name(f"{stem}-{role}{suffix}"))
        except Exception as e:
            # fallback to a simple role-specific name in logs/
            logger.debug(f"Failed to construct role-specific logfile from {base}: {e}")
            return str(Path("logs") / f"agent-{role}.log")

    # Determine logfile: prefer explicit per-role setting if provided in config.
    log_path = config.logging.file_cli or _role_logfile(config.logging.file or "logs/agent.log", "cli")
    log_file = setup_logging(
        config.logging.enabled, 
        config.logging.level, 
        log_path,
        rotation_enabled=config.logging.rotation_enabled,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count
    )
    logger = logging.getLogger(__name__)
    # If verbose not set, reduce console output to WARNING to avoid noisy logs on stdout
    if not args.verbose:
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            # FileHandler is a subclass of StreamHandler — exclude it
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    if log_file:
        logger.info("Logging initialized, file=%s", log_file)
    # Apply SSL bypass if configured
    if not config.network.ssl_verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
    registry = MCPRegistry()
    vprint("[cli] bootstrapping servers...")
    logger.info("Bootstrapping servers")

    # Store config for lazy batch queue manager initialization
    # This allows LLMFactory to create the manager when first needed
    from .llm.factory import set_batch_config
    set_batch_config(config)

    # Use InitializationService for consistent bootstrap + injection
    from .services.initialization_service import InitializationService
    init_service = InitializationService(config)
    registry, session_service = init_service.initialize_for_cli()

    # Keep references to session_manager for CLI use
    session_manager = init_service.session_manager

    vprint(f"[cli] servers registered: {', '.join(registry.list())}")
    logger.info("Servers registered: %s", ", ".join(registry.list()))

    # Initialize global MCP integration to enable tool sharing across agents
    vprint("[cli] initializing MCP integration...")
    logger.info("Initializing MCP integration")
    try:
        asyncio.run(initialize_mcp(config))
        vprint("[cli] MCP integration initialized")
        logger.info("MCP integration initialized successfully")
    except Exception as e:
        logger.warning("Failed to initialize MCP integration: %s", e)
        vprint(f"[cli] Warning: MCP integration failed: {e}")

    # Initialize batch queue manager if any LLM models have batch enabled
    vprint("[cli] initializing batch queue manager...")
    try:
        asyncio.run(init_batch_system(config))
        vprint("[cli] batch queue manager initialized")
        logger.info("Batch queue manager initialized successfully")
    except Exception as e:
        logger.warning("Failed to initialize batch queue manager: %s", e)
        vprint(f"[cli] Warning: batch queue manager failed: {e}")

    # Note: SessionManager, SessionService, and dependency injection
    # are now handled by InitializationService.initialize_for_cli() above

    # Determine CLI agent name from config (can be overridden with --agent)
    entry_name = getattr(args, "agent_override", None) or config.default_agent

    # Get or create the agent
    from .servers.agent.server import Agent as _Agent

    agent = None
    if entry_name in registry.list():
        existing = registry.get(entry_name)
        if isinstance(existing, _Agent):
            agent = existing
            agent.registry = registry  # type: ignore[attr-defined]
            # Update session_service for existing agent
            agent._session_service = session_service  # type: ignore[attr-defined]
        else:
            # Entry exists but is not an Agent (probably a plugin/tool)
            logger.error(f"'{entry_name}' is registered as {type(existing).__name__}, not an Agent")
            print(f"Error: '{entry_name}' is not an agent. It's a {type(existing).__name__}.", file=sys.stderr)
            print("\nAvailable agents:", file=sys.stderr)
            for name in registry.list():
                server = registry.get(name)
                if isinstance(server, _Agent):
                    print(f"  - {name}", file=sys.stderr)
            sys.exit(1)

    if agent is None:
        # Create new agent - need agent_config for this
        logger.info("Creating new Agent instance '%s'", entry_name)
        plugins_cfg = _get_plugins_config(config)
        mcp_config = plugins_cfg.servers.get(entry_name) if plugins_cfg else None
        if not mcp_config or not getattr(mcp_config, 'agent_config', None):
            logger.error(f"Cannot create agent '{entry_name}': no agent_config found in MCP config")
            print(f"Error: Agent '{entry_name}' not found and cannot be created (no agent_config in plugins.yaml).", file=sys.stderr)
            print("\nAvailable agents:", file=sys.stderr)
            for name in registry.list():
                server = registry.get(name)
                if isinstance(server, _Agent):
                    print(f"  - {name}", file=sys.stderr)
            sys.exit(1)

        agent = _Agent(entry_name, config, mcp_config, registry, session_service=session_service)
        registry.register(entry_name, agent)
        vprint(f"[cli] created agent: {entry_name}")

    # Process multimodal attachments (images, audio, text files)
    task_input: Union[str, ChatMessage] = args.task
    has_images = getattr(args, "images", None)
    has_audio = getattr(args, "audio", None)
    has_text_files = getattr(args, "text_files", None)
    
    if has_images or has_audio or has_text_files:
        attachment_counts = []
        if has_images:
            attachment_counts.append(f"{len(has_images)} image(s)")
        if has_audio:
            attachment_counts.append(f"{len(has_audio)} audio(s)")
        if has_text_files:
            attachment_counts.append(f"{len(has_text_files)} text file(s)")
        vprint(f"[cli] processing attachments: {', '.join(attachment_counts)}")
        
        try:
            from .utils.multimodal_processor import (
                create_multimodal_message_extended,
                ImageProcessingError,
                AudioProcessingError,
                TextFileProcessingError
            )

            # Convert string paths to lists of Path objects
            image_paths = [Path(p) for p in has_images] if has_images else None
            audio_paths = [Path(p) for p in has_audio] if has_audio else None
            text_file_paths = [Path(p) for p in has_text_files] if has_text_files else None

            # Create multimodal message with all attachment types
            task_input = create_multimodal_message_extended(
                text=args.task,
                image_paths=image_paths,
                audio_paths=audio_paths,
                text_file_paths=text_file_paths,
                max_size_mb=None  # No hard limit, just warnings
            )

            vprint("[cli] created multimodal message")

        except ImageProcessingError as e:
            print(f"Error processing image: {e}", file=sys.stderr)
            return
        except AudioProcessingError as e:
            print(f"Error processing audio: {e}", file=sys.stderr)
            return
        except TextFileProcessingError as e:
            print(f"Error processing text file: {e}", file=sys.stderr)
            return
        except ImportError as e:
            print(f"Error: Multimodal processing requires Pillow: {e}", file=sys.stderr)
            print("Install with: pip install Pillow", file=sys.stderr)
            return
        except Exception as e:
            print(f"Error processing attachments: {e}", file=sys.stderr)
            logger.exception("Unexpected error in multimodal processing")
            return

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)

    # Extract LLM profile override early (needed in session operations)
    llm_profile_override = getattr(args, "llm_profile_override", None)

    # Initialize session management
    session_id = getattr(args, "session_id", None)
    session_user = getattr(args, "session_user", "cli_user")
    list_sessions = getattr(args, "list_sessions", False)

    # Generate or use provided session ID
    from .utils.id import short_id
    actual_session_id = session_id or short_id()

    # SessionManager and SessionService already initialized earlier (before Agent creation)
    # to enable passing session_service to Agent constructor

    # Helper async function for session operations
    async def handle_session_operations():
        nonlocal actual_session_id
        was_new_session = False  # Track if we're creating a new session

        # Handle --list-sessions flag
        if list_sessions:
            vprint(f"[cli] listing sessions for user: {session_user}")
            try:
                sessions = await session_manager.list_sessions(session_user)

                if not sessions:
                    print(f"No sessions found for user '{session_user}'")
                    return False, was_new_session  # Signal to exit

                print(f"\nSessions for user '{session_user}':")
                print("-" * 80)
                for sess in sessions:
                    sess_id = sess.get("session_id", "unknown")
                    title = sess.get("title", "Untitled")
                    agent_name = sess.get("agent_name", "unknown")
                    llm_profile = sess.get("llm_profile", "unknown")
                    created = sess.get("created_at", "unknown")
                    msg_count = sess.get("message_count", len(sess.get("messages", [])))  # Use message_count from metadata

                    print(f"ID: {sess_id}")
                    print(f"  Title: {title}")
                    print(f"  Agent: {agent_name}, LLM: {llm_profile}")
                    print(f"  Messages: {msg_count}, Created: {created}")
                    print()
                return False, was_new_session  # Signal to exit
            except Exception as e:
                logger.error(f"Failed to list sessions: {e}", exc_info=True)
                print(f"Error listing sessions: {e}", file=sys.stderr)
                return False, was_new_session  # Signal to exit

        # Load existing session if --session provided
        session_exists = False
        if session_id:
            vprint(f"[cli] loading session: {session_id}")
            try:
                session_exists, msg_count = await session_service.load_and_restore_session(
                    agent, session_user, session_id
                )
                if session_exists:
                    vprint(f"[cli] loaded session with {msg_count} messages")
                    logger.info(f"Loaded session {session_id} with {msg_count} messages")
                    was_new_session = False
                else:
                    # Session ID provided but doesn't exist - create it
                    logger.info(f"Session '{session_id}' not found, creating new session with this ID")
                    print(f"Creating new session '{session_id}'")
                    was_new_session = True  # Will be saved at end
                # Initialize session in agent if it doesn't exist
                if hasattr(agent, '_session_tracker'):
                    agent._session_tracker.set_session_messages(actual_session_id, [])
            except SessionPermissionError as e:
                # User trying to access session they don't own
                logger.error(f"Permission denied for session {session_id}: {e}")
                print(f"Error: {e}", file=sys.stderr)
                print("This session belongs to a different user. Use a different session ID.", file=sys.stderr)
                return False, was_new_session  # Signal to exit
            except Exception as e:
                logger.error(f"Failed to load session {session_id}: {e}", exc_info=True)
                print(f"Error loading session: {e}", file=sys.stderr)
                return False, was_new_session  # Signal to exit
        else:
            # No session ID provided - create new one with auto-generated ID
            logger.debug(f"Creating new session: {actual_session_id}")
            was_new_session = True
            if hasattr(agent, '_session_tracker'):
                agent._session_tracker.set_session_messages(actual_session_id, [])

        # CRITICAL: Initialize session template_vars from agent_config for NEW sessions
        # This ensures initial values (like workflow_phase: "planning") are available
        # without requiring explicit set_context calls
        if was_new_session and hasattr(agent, '_session_tracker') and hasattr(agent, 'agent_config'):
            if agent.agent_config and agent.agent_config.template_vars:
                initial_vars = agent.agent_config.template_vars.copy()
                agent._session_tracker.set_session_template_vars(actual_session_id, initial_vars)
                logger.debug(f"[cli] Initialized session template_vars from agent_config: {list(initial_vars.keys())}")

        return True, was_new_session  # Continue with task execution

    # Run session operations
    should_continue, was_new_session = asyncio.run(handle_session_operations())
    if not should_continue:
        return

    async def _stream_and_run_with_status(
        agent: Agent,
        task: Union[str, ChatMessage],
        session_id: str,  # Add session_id parameter
        show_mcp: bool = False,
        show_status: bool = True,
        llm_override=None,
        llm_profile_info: Optional[str] = None
    ) -> dict:
        """Stream and run agent with status display.

        Args:
            agent: The agent to run
            task: Either a string task or ChatMessage with multimodal content
            show_mcp: Whether to show MCP call details
            show_status: Whether to show status events
            llm_override: Optional LLM client to override agent's default
            llm_profile_info: Optional profile info string for logging
        """
        # Extract task text for logging
        if isinstance(task, ChatMessage):
            if isinstance(task.content, str):
                task_text = task.content
            elif isinstance(task.content, list):
                # Content items are Pydantic models, use attribute access
                text_parts = [getattr(item, "text", "") for item in task.content if hasattr(item, "type") and getattr(item, "type") == "text"]
                task_text = " ".join(text_parts) if text_parts else "[multimodal input]"
            else:
                task_text = "[multimodal input]"
        else:
            task_text = task

        final_result: Dict[str, Any] = {"task": task_text, "calls": []}

        # Subscribe to status events if enabled
        status_queue = None
        if show_status:
            status_queue = await status_bus.subscribe()

        # Optionally auto-subscribe to external SSE status stream
        sse_task = None
        sse_url = os.environ.get("AGENT_STATUS_SSE_STREAM_URL")

        async def _status_subscriber():
            """Subscribe to local status events and display them"""
            if not status_queue:
                return
            try:
                while True:
                    event = await status_queue.get()
                    # Display status event in a clean format using new StatusEvent format
                    phase = event.phase.value if hasattr(event.phase, 'value') else str(event.phase)
                    phase_disp = phase
                    if _supports_color():
                        phase_color_map = {
                            "start": "36",      # cyan
                            "progress": "34",   # blue
                            "end": "32",        # green
                            "error": "31",      # red
                        }
                        c = phase_color_map.get(phase, "34")
                        phase_disp = _colorize(phase, c)

                    server_col = event.server
                    txt = event.message
                    status_line = f"[{phase_disp}] {server_col}: {txt}"

                    # Error phase should be red
                    if phase == "error" and _supports_color():
                        status_line = _colorize(status_line, "31")
                    print(status_line)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"Status subscriber error: {e}")
                return

        async def _sse_subscriber(url: str):
            try:
                try:
                    import aiohttp
                except Exception:
                    return
                timeout = aiohttp.ClientTimeout(total=None)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url) as resp:
                        if resp.status != 200:
                            return
                        async for line in resp.content:
                            try:
                                text = line.decode("utf-8").strip()
                            except Exception:
                                continue
                            if not text:
                                continue
                            if text.startswith("data:"):
                                payload = text[len("data:"):].strip()
                                try:
                                    obj = json.loads(payload)
                                except Exception as e:
                                    logger.debug(f"Failed to parse SSE payload: {e}")
                                    obj = {"raw": payload}
                                # Print SSE messages in short form
                                if _supports_color():
                                    print(_colorize(f"[SSE] {obj.get('server','?')}: {obj.get('message','')}", "34"))
                                else:
                                    print(f"[SSE] {obj.get('server','?')}: {obj.get('message','')}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"SSE subscriber error: {e}")
                return

        # Start status subscriber task if enabled
        status_task = None
        if show_status and status_queue:
            status_task = asyncio.create_task(_status_subscriber())

        if sse_url:
            try:
                sse_task = asyncio.create_task(_sse_subscriber(sse_url))
            except Exception as e:
                logger.warning(f"Failed to create SSE subscriber task: {e}", exc_info=True)
                sse_task = None
        try:
            async for ev in agent.run_events(task, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                t = ev.get("type")
                if t == "mcp_call" and show_mcp:
                    srv = ev.get("server")
                    action = ev.get("action")
                    params = ev.get("params") or {}
                    # Human readable print
                    header = f"MCP CALL -> server={srv} action={action}"
                    if _supports_color():
                        header = _colorize(header, "36")
                    print(header)
                    print(json.dumps(params, indent=2, ensure_ascii=False))
                elif t == "mcp_result":
                    srv = ev.get("server")
                    action = ev.get("action")
                    res = ev.get("result")
                    # Append to final_result calls for JSON output
                    final_result.setdefault("calls", []).append({"server": srv, "action": action, "result": res})
                    if show_mcp:
                        header = f"MCP RESULT <- server={srv} action={action}"
                        if _supports_color():
                            header = _colorize(header, "32")
                        print(header)
                        try:
                            print(json.dumps(res, indent=2, ensure_ascii=False))
                        except Exception as e:
                            logger.debug(f"Failed to JSON dump MCP result: {e}")
                            print(str(res))
                elif t == "thinking_delta":
                    # Show thinking/reasoning content as it streams (like WebUI)
                    delta = ev.get("delta", "")
                    if delta:
                        # Print without newline for streaming effect
                        if _supports_color():
                            print(_colorize(delta, "90"), end="", flush=True)  # Dark gray
                        else:
                            print(delta, end="", flush=True)
                elif t == "thinking_complete":
                    # Thinking finished - add newline
                    print()  # Newline after thinking content
                elif t == "thinking":
                    # Optionally show LLM progress when verbose (backward compatibility)
                    if args.verbose:
                        step = ev.get("step")
                        print(f"[LLM] thinking (step {step})")
                elif t == "final":
                    # Store final summary in result AND print it immediately for streaming
                    summary = ev.get("summary")
                    if summary:
                        final_result["summary"] = summary
                        # Print summary immediately during streaming (don't wait for end)
                        print("", flush=True)  # Newline before summary
                        try:
                            # Use the formatting function for consistent ANSI output
                            formatted_summary, content_format = await format_output_with_hooks(
                                output=summary,
                                agent_instance=agent,
                                session_id=actual_session_id,
                                request_id="cli_display",
                                output_format='ansi'  # Request ANSI format for terminal display
                            )
                            if content_format == 'ansi':
                                render_with_rich(formatted_summary)
                            else:
                                line = f"{formatted_summary}"
                                print(line, flush=True)
                        except Exception as e:
                            # Fallback to plain text
                            logger.debug(f"Failed to format summary: {e}")
                            print(f"{summary}", flush=True)
                elif t == "error":
                    err = f"ERROR: {ev.get('message')}"
                    if _supports_color():
                        err = _colorize(err, "31")
                    print(err)
                elif t == "cancelled":
                    # Agent was cancelled (Ctrl-C or timeout)
                    msg = "\n✋ Cancelled by user"
                    if _supports_color():
                        msg = _colorize(msg, "33")  # yellow
                    print(msg)
                    final_result["cancelled"] = True
                elif t == "done":
                    # run_events may emit a final aggregated result
                    fr = ev.get("result")
                    if isinstance(fr, dict):
                        final_result = fr
                # keep looping until 'end'

            return final_result
        except (asyncio.CancelledError, KeyboardInterrupt):
            # User pressed Ctrl-C: provide clean exit message
            msg = "\n✋ Cancelled by user"
            if _supports_color():
                msg = _colorize(msg, "33")  # yellow
            print(msg)
            return {"task": task, "cancelled": True, "summary": final_result.get("summary", "")}
        except Exception as e:
            # Fallback: surface exception as result
            return {"task": task, "errors": [str(e)]}
        finally:
            # Cleanup background tasks
            # Drain any queued status events deterministically before cancelling
            # the background status subscriber. This avoids a race where the
            # final PHASE_END is published but the subscriber is cancelled
            # before it can process the queued event.
            if status_queue:
                try:
                    while not status_queue.empty():
                        try:
                            event = status_queue.get_nowait()
                        except Exception as e:
                            logger.debug(f"Failed to get status event from queue: {e}")
                            break
                        # Reuse the same display logic as _status_subscriber
                        phase = getattr(event, "phase", "progress")
                        phase_disp = phase
                        if _supports_color():
                            phase_color_map = {
                                "start": "36",
                                "progress": "34",
                                "end": "32",
                                "error": "31",
                            }
                            c = phase_color_map.get(phase, "34")
                            phase_disp = _colorize(phase, c)

                        server_col = event.server
                        txt = event.message
                        status_line = f"[{phase_disp}] {server_col}: {txt}"

                        if event.level == "error" and _supports_color():
                            status_line = _colorize(status_line, "31")
                        elif event.level == "warning" and _supports_color():
                            status_line = _colorize(status_line, "33")
                        print(status_line)
                except Exception as e:
                    # If anything goes wrong while draining, continue to cancel tasks
                    logger.debug(f"Exception while draining status queue: {e}")
            if status_task and not status_task.done():
                try:
                    status_task.cancel()
                except Exception as e:
                    logger.debug(f"Failed to cancel status task: {e}")
                    pass
            if sse_task and not sse_task.done():
                try:
                    sse_task.cancel()
                except Exception:
                    pass

    # Execute with new status-aware streaming
    show_mcp = getattr(args, "show_mcp", False)
    show_status = not getattr(args, "no_status", False)

    # Create LLM override if --llm was specified
    llm_override = None
    llm_profile_info = None
    llm_profile_override = getattr(args, "llm_profile_override", None)

    if llm_profile_override:
        if config.llm_system and config.llm_system.profiles:
            if llm_profile_override not in config.llm_system.profiles:
                available_profiles = sorted(config.llm_system.profiles.keys())
                error_msg = f"ERROR: LLM profile '{llm_profile_override}' not found in configuration."
                if available_profiles:
                    error_msg += "\n\nAvailable profiles:\n  " + "\n  ".join(available_profiles)
                print(error_msg, file=sys.stderr)
                return

            try:
                # Use factory function that properly handles batch mode
                from .llm.factory import create_llm_from_profile, resolve_llm_config_for_agent
                from .config.models import AgentConfig

                llm_override = create_llm_from_profile(
                    config=config,
                    llm_profile=llm_profile_override,
                )

                # Get profile info for logging
                temp_agent_config = AgentConfig(llm_profile=llm_profile_override)
                llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
                model = llm_kwargs.get('model', 'unknown')
                provider = llm_kwargs.get('provider', 'unknown')
                llm_profile_info = f"{llm_profile_override}:{provider}/{model}"

                logger.info(f"Using LLM override: {llm_profile_info}")
                vprint(f"[cli] Using LLM profile: {llm_profile_info}")
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                print(f"ERROR: Failed to apply LLM profile '{llm_profile_override}': {str(e)}", file=sys.stderr)
                return

    # Set session metadata for tool execution context (enables _user_id, _agent injection)
    if hasattr(agent, '_session_tracker'):
        # Determine effective LLM profile (override or agent default)
        effective_llm_profile = llm_profile_override or agent.agent_config.default_llm_profile

        agent._session_tracker.set_session_metadata(actual_session_id, {
            "user_id": session_user,
            "agent_name": entry_name,
            "llm_profile": effective_llm_profile
        })

    try:
        if getattr(args, "raw", False):
            # Raw mode: use run_events with result collection
            from .servers.agent.result_utils import collect_final_result

            result = asyncio.run(collect_final_result(agent, task_input, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info))
        else:
            result = asyncio.run(_stream_and_run_with_status(agent, task_input, actual_session_id, show_mcp=show_mcp, show_status=show_status, llm_override=llm_override, llm_profile_info=llm_profile_info))

        # Check if request was cancelled
        if result.get("cancelled", False):
            vprint("[cli] cancelled by user")
            logger.info("Request cancelled by user")
        else:
            vprint("[cli] done")
            logger.info("Task completed")

        # Save session after successful task execution (skip if cancelled)
        async def save_session_after_task():
            try:
                # Use the actual agent name that was requested (entry_name from args)
                # instead of agent.agent_name which may not exist or be "default"
                agent_name_used = entry_name  # The agent name determined from args.agent_override or config.default_agent
                llm_profile_used = getattr(args, "llm_profile_override", None) or "normal"

                # Save the session
                success = await session_service.save_session(
                    agent=agent,
                    user_id=session_user,
                    session_id=actual_session_id,
                    agent_name=agent_name_used,
                    llm_profile=llm_profile_used,
                    was_new_session=was_new_session
                )

                if success:
                    if session_id:
                        vprint(f"[cli] updated session: {session_id}")
                        logger.info(f"Updated session {session_id}")
                    else:
                        vprint(f"[cli] created new session: {actual_session_id}")
                        logger.info(f"Created new session {actual_session_id}")
                        print(f"\nSession saved: {actual_session_id}")
                else:
                    logger.warning("Session save returned False")
            except Exception as e:
                logger.error(f"Failed to save session: {e}", exc_info=True)
                print(f"Warning: Failed to save session: {e}", file=sys.stderr)

        # Only save session if not cancelled
        if not result.get("cancelled", False):
            asyncio.run(save_session_after_task())

    finally:
        # Shutdown batch queue manager first
        try:
            asyncio.run(shutdown_batch_system())
            vprint("[cli] batch queue manager shut down")
            logger.info("Batch queue manager shut down successfully")
        except Exception as e:
            logger.warning("Failed to shutdown batch queue manager: %s", e)
        
        # Ensure MCP integration is properly shut down to close aiohttp sessions
        try:
            asyncio.run(shutdown_mcp())
            vprint("[cli] MCP integration shut down")
            logger.info("MCP integration shut down successfully")
        except Exception as e:
            logger.warning("Failed to shutdown MCP integration: %s", e)

    # Human-readable final output
    def _pretty_print_result(res: dict, show_mcp: bool = False, agent_instance=None, session_id_val: str = "unknown", skip_summary: bool = False) -> None:
        # Calls (print first so summary appears at the end, only when show_mcp is True)
        calls = res.get("calls", []) or []
        if calls and show_mcp:
            print("")
            print("Tool calls:")
            for c in calls:
                srv = c.get("server")
                action = c.get("action")
                header = f"- {srv} :: {action}"
                if _supports_color():
                    header = _colorize(header, "36")
                print(header)
                result_obj = c.get("result")
                # Render result as YAML for human readability when possible
                try:
                    yaml_text = yaml.safe_dump(result_obj, allow_unicode=True, sort_keys=False)
                    for line in yaml_text.rstrip().splitlines():
                        print(f"    {line}")
                except Exception as e:
                    # Fallback to JSON-ish string
                    logger.debug(f"Failed to YAML dump result: {e}")
                    try:
                        j = json.dumps(result_obj, ensure_ascii=False)
                        print(f"    {j}")
                    except Exception as e2:
                        logger.debug(f"Failed to JSON dump result: {e2}")
                        print(f"    {str(result_obj)}")

        # Summary (print after calls so it is the final user-visible result)
        # Skip if skip_summary=True (already printed during streaming)
        summary = None
        if not skip_summary:
            summary = res.get("summary")

        if summary:
            # Format summary using FORMAT_OUTPUT hooks if available (ANSI for terminal)
            formatted_summary = summary
            content_format = 'text'

            try:
                # Use central ANSI formatting function (respects --color flag)
                import asyncio

                # Get or create event loop
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                formatted_summary, content_format = loop.run_until_complete(
                    format_output_with_hooks(
                        output=summary,
                        agent_instance=agent_instance,
                        session_id=session_id_val,
                        request_id="cli_display"
                    )
                )
                logger.info(f"Formatted summary: format={content_format}, length={len(formatted_summary)}")
                vprint(f"[cli] Formatted summary: format={content_format}, length={len(formatted_summary)}")

            except Exception as e:
                logger.warning(f"Failed to format summary with ANSI: {e}", exc_info=True)
                vprint(f"[cli] ERROR formatting summary: {e}")

            print("")
            if content_format == 'ansi':
                render_with_rich(formatted_summary)
            else:
                line = f"Summary: {formatted_summary}"
                if _supports_color():
                    line = _colorize(line, "33")
                print(line)

        # Errors
        errors = res.get("errors") or []
        if errors:
            print("")
            print(_colorize("Errors:", "31") if _supports_color() else "Errors:")
            for e in errors:
                print(f"  - {e}")

        # If verbose, print raw JSON for debugging
        if getattr(args, "verbose", False):
            print("")
            print(_colorize("Raw result JSON:", "35") if _supports_color() else "Raw result JSON:")
            print(json.dumps(res, indent=2, ensure_ascii=False))

    # If raw requested, print JSON and exit. Ensure output is flushed so
    # test harnesses and non-interactive environments capture it.
    if getattr(args, "raw", False):
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    else:
        # Skip summary in pretty print since it was already printed during streaming
        _pretty_print_result(result, show_mcp=show_mcp, agent_instance=agent, session_id_val=actual_session_id, skip_summary=True)
        try:
            sys.stdout.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
