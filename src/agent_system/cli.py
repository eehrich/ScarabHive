from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml
from typing import Any, Dict, List, Tuple, Union

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

from .config.settings import load_settings
from .config.models import MCPConfig, AgentSystemConfig
from .llm.models import ChatMessage
from .plugins import discover_all_plugins
from .mcp.base import MCPRegistry
from .mcp.status import status_bus
from .mcp.integration import MCPIntegration, initialize_mcp, shutdown_mcp
from .utils.logging import setup_logging
from .servers.bootstrap import bootstrap_servers
from .servers.agent.server import Agent

# Import services
from .services import MCPService, ToolService


def _get_plugins_config(config: AgentSystemConfig):
    """Get plugins configuration."""
    return config.plugins


def _get_server_mode_config(config: AgentSystemConfig):
    """Get server_mode configuration."""
    return config.server_mode


def _get_agents_dict(config: AgentSystemConfig):
    """Get agents configuration dict."""
    return config.agents if config.agents else {}


# Global color mode: tests may monkeypatch this variable
color_mode: str = "auto"

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
                
                async with httpx.AsyncClient(timeout=5.0) as client:
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


def _supports_color() -> bool:
    """Return whether ANSI color sequences should be used.

    Honors the global `color_mode` which tests may set to 'auto',
    'always' or 'never'. In 'auto' mode this checks stdout.isatty().
    """
    if color_mode == "never":
        return False
    if color_mode == "always":
        return True
    # auto
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _colorize(text: str, color_code: str) -> str:
    """Wrap text in ANSI color codes when supported."""
    if not _supports_color():
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"


def _register_config_agents_commands(subparsers: Any) -> None:
    """Register config-agents CLI commands (Epic 0043) - inline implementation."""
    # Main config-agents command group
    config_agents_parser = subparsers.add_parser(
        "config-agents",
        help="Manage configuration-based agents"
    )
    config_agents_subparsers = config_agents_parser.add_subparsers(dest="config_agents_cmd")
    
    # config-agents list
    list_parser = config_agents_subparsers.add_parser(
        "list",
        help="List all configuration-based agents"
    )
    list_parser.add_argument(
        "--format",
        dest="out_format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)"
    )
    
    # config-agents show
    show_parser = config_agents_subparsers.add_parser(
        "show",
        help="Show detailed information about a config agent"
    )
    show_parser.add_argument(
        "agent_name",
        help="Name of the configuration-based agent"
    )
    show_parser.add_argument(
        "--format",
        dest="out_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )
    
    # config-agents validate
    validate_parser = config_agents_subparsers.add_parser(
        "validate",
        help="Validate configuration-based agents"
    )
    validate_parser.add_argument(
        "agent_name",
        nargs="?",
        help="Name of the agent to validate (if not specified, validates all)"
    )
    validate_parser.add_argument(
        "--format",
        dest="out_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )


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
    # color can be set to auto/always/never; --no-color is alias for never
    prelim.add_argument("--color", dest="color", choices=["auto", "always", "never"], default="always")
    prelim.add_argument("--no-color", dest="no_color", action="store_true")
    prelim.add_argument("--show-mcp", dest="show_mcp", action="store_true")
    prelim.add_argument("--no-status", dest="no_status", action="store_true")
    prelim.add_argument("--raw", dest="raw", action="store_true")
    orig_args = sys.argv[1:]
    ns, rest = prelim.parse_known_args(orig_args)

    # decide color mode early so helpers behave predictably
    global color_mode
    if getattr(ns, "no_color", False):
        color_mode = "never"
    else:
        color_mode = getattr(ns, "color", "auto")

    # Initialize colorama on interactive TTYs so ANSI renders on Windows
    try:
        if color_mode != "never" and sys.stdout.isatty():
            import colorama
            colorama.init()
    except Exception:
        pass

    # If the first token of the remaining args isn't a known subcommand, insert implicit 'run'
    known = ("plugins", "mcp", "run", "status", "users", "config-agents", "-h", "--help")
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
    parser.add_argument("--color", dest="color", choices=["auto", "always", "never"], default="always", help="Colorize output (auto|always|never)")
    parser.add_argument("--no-color", dest="no_color", action="store_true", help="Disable color output (alias for --color never)")
    parser.add_argument("--show-mcp", dest="show_mcp", action="store_true", help="Show MCP call/result details (for debugging)")
    parser.add_argument("--no-status", dest="no_status", action="store_true", help="Hide status messages during execution")
    parser.add_argument("--raw", dest="raw", action="store_true", help="Output raw JSON result instead of pretty printing")
    subparsers = parser.add_subparsers(dest="subcommand")

    # run subcommand (default behavior)
    run_parser = subparsers.add_parser("run", help="Run an agent task (default)")
    run_parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")
    run_parser.add_argument("--images", "--attach", dest="images", nargs="+", metavar="PATH", help="Path(s) to image file(s) to attach to the task")


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

    # config-agents subcommand for configuration-based agents (Epic 0043)
    _register_config_agents_commands(subparsers)

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
            out = []
            for name, factory in plugins.items():
                meta = getattr(factory, "_plugin_metadata", None) or {}
                # metadata loading is handled centrally in discover_all_plugins();
                # keep local code minimal.
                # include whether this plugin is enabled in the current config
                plugins_cfg = _get_plugins_config(config)
                enabled_servers = [k for k, v in plugins_cfg.servers.items() if v.enabled] if plugins_cfg else []
                enabled_flag = name in enabled_servers
                out.append({
                    "name": name,
                    "description": meta.get("description"),
                    "version": meta.get("version"),
                    "enabled": enabled_flag,
                })
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
                rows.append((p.get("name") or "", display_enabled, p.get("description") or "", p.get("version") or ""))
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

    # Handle config-agents subcommand (Epic 0043)
    if args.subcommand == "config-agents":
        from .plugins.config_agent_discovery import list_config_agents, get_config_agent_info
        from .plugins.config_agent_validation import (
            validate_all_config_agents,
            validate_config_agent,
            get_validation_summary
        )
        
        cmd = getattr(args, "config_agents_cmd", None)
        
        if cmd == "list":
            # List all config agents
            agents = list_config_agents(config.agents)
            
            if args.out_format == "json":
                print(json.dumps(agents, indent=2, ensure_ascii=False))
            else:
                # Table format
                if not agents:
                    print("No configuration-based agents found.")
                    return

                rows = []
                for agent in agents:
                    name = agent.get('name', 'unknown')
                    enabled = agent.get('enabled', False)
                    llm_profile = agent.get('llm_profile', 'N/A')
                    max_steps = agent.get('max_steps', 'N/A')
                    description = agent.get('description', '')
                    status = "Enabled" if enabled else "Disabled"
                    
                    # Colorize status
                    if _supports_color():
                        if enabled:
                            status = _colorize(status, "32")  # green
                        else:
                            status = _colorize(status, "90")  # gray
                    
                    rows.append((name, llm_profile, max_steps, status, description))

                headers = ["NAME", "LLM", "STEPS", "STATUS", "DESCRIPTION"]
                if tabulate:
                    print(tabulate(rows, headers=headers, tablefmt="github"))
                else:
                    # Fallback if tabulate not available
                    print(f"{headers[0]:<20} {headers[1]:<10} {headers[2]:<8} {headers[3]:<10} {headers[4]}")
                    print("-" * 80)
                    for row in rows:
                        print(f"{row[0]:<20} {row[1]:<10} {str(row[2]):<8} {row[3]:<10} {row[4][:30]}")
            return
        
        elif cmd == "show":
            # Show detailed information about a specific config agent
            agent_name = getattr(args, "agent_name", None)
            
            if not agent_name:
                print(json.dumps({"error": "agent_name required"}, ensure_ascii=False))
                return
            
            try:
                info = get_config_agent_info(agent_name, config.agents)
            except KeyError:
                print(json.dumps({"error": f"Config agent '{agent_name}' not found"}, ensure_ascii=False))
                return
            
            if args.out_format == "json":
                print(json.dumps(info, indent=2, ensure_ascii=False))
            else:
                # Human-readable format
                print(f"\n{'=' * 60}")
                print(f"Config Agent: {info['name']}")
                print(f"{'=' * 60}")
                
                # Status with color
                enabled_text = "Enabled" if info['enabled'] else "Disabled"
                if _supports_color():
                    enabled_text = _colorize(enabled_text, "32" if info['enabled'] else "90")
                print(f"Status:       {enabled_text}")
                
                print(f"Description:  {info.get('description', 'N/A')}")
                print(f"Base Type:    {info['base_type']}")
                print(f"LLM Profile:  {info['llm_profile']}")
                print(f"Max Steps:    {info['max_steps']}")
                
                if info.get('system_template'):
                    print(f"Template:     {info['system_template']}")
                if info.get('has_inline_prompt'):
                    print("Prompt:       [Inline prompt defined]")
                
                if info.get('tools'):
                    print("\nTools:")
                    allowed = info['tools'].get('allowed', [])
                    blocked = info['tools'].get('blocked', [])
                    if allowed:
                        print(f"  Allowed:  {', '.join(allowed)}")
                    else:
                        print("  Allowed:  (none)")
                    if blocked:
                        print(f"  Blocked:  {', '.join(blocked)}")
                
                if info.get('context_management'):
                    ctx = info['context_management']
                    print("\nContext Management:")
                    print(f"  Enabled:   {ctx.get('enabled', False)}")
                    if ctx.get('enabled'):
                        print(f"  Strategy:  {ctx.get('strategy', 'N/A')}")
                        print(f"  Preserve:  {ctx.get('preserve_recent_messages', 'N/A')} messages")
                
                if info.get('metadata'):
                    meta = info['metadata']
                    if meta:
                        print("\nMetadata:")
                        for key, value in meta.items():
                            if isinstance(value, list):
                                print(f"  {key}: {', '.join(value)}")
                            else:
                                print(f"  {key}: {value}")
                
                print(f"{'=' * 60}\n")
            return
        
        elif cmd == "validate":
            # Validate config agents
            agent_name = getattr(args, "agent_name", None)
            
            # Get LLM profiles for validation
            llm_profiles = list(config.llm_system.profiles.keys()) if config.llm_system else None
            
            if agent_name:
                # Validate single agent
                agents_dict = _get_agents_dict(config)
                if not agents_dict or agent_name not in agents_dict:
                    print(json.dumps({"error": f"Config agent '{agent_name}' not found"}, ensure_ascii=False))
                    return
                
                definition = agents_dict[agent_name]
                errors = validate_config_agent(agent_name, definition, llm_profiles)
                
                if args.out_format == "json":
                    print(json.dumps({
                        "agent_name": agent_name,
                        "valid": len(errors) == 0,
                        "errors": errors
                    }, indent=2, ensure_ascii=False))
                else:
                    if not errors:
                        success_msg = f"✅ Config agent '{agent_name}' is valid"
                        if _supports_color():
                            success_msg = _colorize(success_msg, "32")
                        print(success_msg)
                    else:
                        error_msg = f"❌ Config agent '{agent_name}' has {len(errors)} error(s):"
                        if _supports_color():
                            error_msg = _colorize(error_msg, "31")
                        print(error_msg)
                        for i, error in enumerate(errors, 1):
                            print(f"  {i}. {error}")
                        sys.exit(1)
            else:
                # Validate all agents
                results = validate_all_config_agents(config.agents, llm_profiles)
                
                if args.out_format == "json":
                    output = {
                        "total": len(results),
                        "passed": sum(1 for errors in results.values() if not errors),
                        "failed": sum(1 for errors in results.values() if errors),
                        "results": [
                            {"agent_name": name, "valid": len(errors) == 0, "errors": errors}
                            for name, errors in results.items()
                        ]
                    }
                    print(json.dumps(output, indent=2, ensure_ascii=False))
                else:
                    summary = get_validation_summary(results)
                    print(summary)
                    
                    # Exit with error if any agent failed
                    if any(errors for errors in results.values()):
                        sys.exit(1)
            return
        else:
            # No valid subcommand - show help
            print("Usage: config-agents {list|show|validate}", file=sys.stderr)
            sys.exit(1)

    # Handle users management subcommand
    if args.subcommand == "users":
        # Import typer-based user CLI using importlib to avoid package/module name conflict
        # (cli.py and cli/ directory both exist)
        import importlib.util
        
        # Dynamically import users module from cli/ directory
        users_module_path = Path(__file__).parent / "cli" / "users.py"
        spec = importlib.util.spec_from_file_location("agent_system.cli.users", users_module_path)
        if spec and spec.loader:
            users_module = importlib.util.module_from_spec(spec)
            sys.modules["agent_system.cli.users"] = users_module
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
    log_file = setup_logging(config.logging.enabled, config.logging.level, log_path)
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
    bootstrap_servers(config, registry)
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
    
    # Determine CLI agent name from config
    entry_name = config.default_agent

    # Get or create the agent
    from .servers.agent.server import Agent as _Agent
    
    agent = None
    if entry_name in registry.list():
        existing = registry.get(entry_name)
        if isinstance(existing, _Agent):
            agent = existing
            agent.registry = registry  # type: ignore[attr-defined]
    
    if agent is None:
        # Create new agent
        logger.info("Creating new Agent instance '%s'", entry_name)
        plugins_cfg = _get_plugins_config(config)
        mcp_config = plugins_cfg.servers.get(entry_name) if plugins_cfg else None
        if not mcp_config:
            logger.warning("No MCPConfig found for agent '%s', using default_config", entry_name)
            mcp_config = plugins_cfg.default_config if plugins_cfg else MCPConfig()
        
        agent = _Agent(entry_name, config, mcp_config, registry)
        registry.register(entry_name, agent)
        vprint(f"[cli] created agent: {entry_name}")

    # Process image attachments if provided
    task_input: Union[str, ChatMessage] = args.task
    if getattr(args, "images", None):
        vprint(f"[cli] processing {len(args.images)} image attachment(s)")
        try:
            from .utils.image_processor import create_multimodal_message, ImageProcessingError
            
            # Convert string paths to Path objects
            image_paths = [Path(img_path) for img_path in args.images]
            
            # Create multimodal message with proper error handling
            task_input = create_multimodal_message(
                text=args.task,
                image_paths=image_paths,
                max_size_mb=None  # No hard limit, just warnings
            )
            
            vprint(f"[cli] created multimodal message with {len(image_paths)} image(s)")
            
        except ImageProcessingError as e:
            print(f"Error: {e}", file=sys.stderr)
            return
        except ImportError as e:
            print(f"Error: Image processing requires Pillow: {e}", file=sys.stderr)
            print("Install with: pip install Pillow", file=sys.stderr)
            return
        except Exception as e:
            print(f"Error processing images: {e}", file=sys.stderr)
            logger.exception("Unexpected error in image processing")
            return

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)
    
    async def _stream_and_run_with_status(
        agent: Agent, 
        task: Union[str, ChatMessage], 
        show_mcp: bool = False, 
        show_status: bool = True
    ) -> dict:
        """Stream and run agent with status display.
        
        Args:
            agent: The agent to run
            task: Either a string task or ChatMessage with multimodal content
            show_mcp: Whether to show MCP call details
            show_status: Whether to show status events
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
            async for ev in agent.run_events(task):
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
                elif t == "thinking":
                    # Optionally show LLM progress when verbose
                    if args.verbose:
                        step = ev.get("step")
                        print(f"[LLM] thinking (step {step})")
                elif t == "final":
                    # buffer final summary; don't print immediately to avoid mid-stream placement
                    summary = ev.get("summary")
                    if summary:
                        final_result["summary"] = summary
                elif t == "error":
                    err = f"ERROR: {ev.get('message')}"
                    if _supports_color():
                        err = _colorize(err, "31")
                    print(err)
                elif t == "done":
                    # run_events may emit a final aggregated result
                    fr = ev.get("result")
                    if isinstance(fr, dict):
                        final_result = fr
                # keep looping until 'end'

            return final_result
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

    try:
        if getattr(args, "raw", False):
            # Raw mode: use run_events with result collection
            from .servers.agent.result_utils import collect_final_result
            
            result = asyncio.run(collect_final_result(agent, task_input))
        else:
            result = asyncio.run(_stream_and_run_with_status(agent, task_input, show_mcp=show_mcp, show_status=show_status))
        vprint("[cli] done")
        logger.info("Task completed")
    finally:
        # Ensure MCP integration is properly shut down to close aiohttp sessions
        try:
            asyncio.run(shutdown_mcp())
            vprint("[cli] MCP integration shut down")
            logger.info("MCP integration shut down successfully")
        except Exception as e:
            logger.warning("Failed to shutdown MCP integration: %s", e)

    # Human-readable final output
    def _pretty_print_result(res: dict, show_mcp: bool = False) -> None:
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
        summary = res.get("summary")
        if summary:
            print("")
            line = f"Summary: {summary}"
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
        _pretty_print_result(result, show_mcp=show_mcp)
        try:
            sys.stdout.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
