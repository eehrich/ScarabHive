"""MCP server management commands for the CLI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from agent_system.mcp.integration import MCPIntegration
from agent_system.cli.utils import _atomic_write_text, _supports_color, _colorize

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

logger = logging.getLogger(__name__)


async def _maybe_await_get_client(mcp_integration: MCPIntegration, name: str):
    """Call client_manager.get_client(name) and await if it returns a coroutine.

    Some implementations expose `get_client` as a coroutine (tests may use
    AsyncMock), while others provide a synchronous method. This helper
    abstracts that difference so callers can `await _maybe_await_get_client(..)`.
    """
    try:
        res = mcp_integration.client_manager.get_client(name)
    except Exception:
        # If attribute access raises, propagate None
        return None
    import inspect
    if inspect.isawaitable(res):
        try:
            return await res
        except Exception:
            return None
    return res


async def _mcp_list_servers(mcp_integration: MCPIntegration, args: Any) -> None:
    """List configured external MCP servers."""
    servers = []

    # List servers from configuration
    for name, server_config in mcp_integration.mcp_config.servers.items():
        # Check if there's a connected client
        client = await _maybe_await_get_client(mcp_integration, name)
        is_connected = client is not None

        server_info = {
            "name": name,
            "address": server_config.url,
            "connected": is_connected,
            "enabled": server_config.enabled,
            "description": server_config.description or ""
        }
        servers.append(server_info)

    if args.out_format == "json":
        print(json.dumps(servers, indent=2, ensure_ascii=False))
    else:
        # Table format
        if not servers:
            print("No external MCP servers configured.")
            return

        rows = []
        for server in servers:
            status = "Connected" if server["connected"] else "Disconnected"
            if not server["enabled"]:
                status = "Disabled"

            if _supports_color():
                if server["connected"]:
                    status = _colorize(status, "32")  # green
                elif server["enabled"]:
                    status = _colorize(status, "31")  # red
                else:
                    status = _colorize(status, "90")  # gray

            rows.append((server["name"], server["address"], status, server["description"]))

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


async def _mcp_connect_server(mcp_integration: MCPIntegration, server_name: str, args: Any) -> None:
    """Connect to an external MCP server."""
    if server_name not in mcp_integration.mcp_config.servers:
        print(json.dumps({"error": f"Server {server_name} not found in configuration"}, ensure_ascii=False))
        return

    server_config = mcp_integration.mcp_config.servers[server_name]
    if not server_config.enabled:
        print(json.dumps({"error": f"Server {server_name} is disabled in configuration"}, ensure_ascii=False))
        return

    try:
        # Create client config
        client_config = {
            "transport": server_config.transport_type,
            "url": server_config.url
        }

        # Add initialization options if present
        if server_config.initialization_options:
            client_config["initialization_options"] = server_config.initialization_options

        await mcp_integration.client_manager.add_client(server_name, client_config)
        print(json.dumps({"result": "connected", "server": server_name}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"Failed to connect to {server_name}: {str(e)}"}, ensure_ascii=False))


async def _mcp_disconnect_server(mcp_integration: MCPIntegration, server_name: str, args: Any) -> None:
    """Disconnect from an external MCP server."""
    try:
        await mcp_integration.client_manager.remove_client(server_name)
        print(json.dumps({"result": "disconnected", "server": server_name}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"Failed to disconnect from {server_name}: {str(e)}"}, ensure_ascii=False))


async def _mcp_status_servers(mcp_integration: MCPIntegration, server_name: str | None, args: Any) -> None:
    """Show status of external MCP servers."""
    if server_name:
        # Status for specific server
        if server_name not in mcp_integration.mcp_config.servers:
            print(json.dumps({"error": f"Server {server_name} not found in configuration"}, ensure_ascii=False))
            return

        server_config = mcp_integration.mcp_config.servers[server_name]
        client = await _maybe_await_get_client(mcp_integration, server_name)
        is_connected = client is not None

        status_info = {
            "name": server_name,
            "connected": is_connected,
            "enabled": server_config.enabled,
            "address": server_config.url,
            "description": server_config.description or ""
        }

        if client and is_connected:
            # Get additional status info if available
            try:
                tools = await client.list_tools()
                status_info["tools_count"] = len(tools) if tools else 0
                status_info["tools"] = [tool.name for tool in tools] if tools else []
            except Exception as e:
                status_info["tools_error"] = str(e)

        print(json.dumps(status_info, indent=2, ensure_ascii=False))
    else:
        # Status for all servers
        await _mcp_list_servers(mcp_integration, args)


async def _mcp_test_server(mcp_integration: MCPIntegration, server_name: str, args: Any) -> None:
    """Test connectivity and basic functionality of an external MCP server."""
    if server_name not in mcp_integration.mcp_config.servers:
        print(json.dumps({"error": f"Server {server_name} not found in configuration"}, ensure_ascii=False))
        return

    server_config = mcp_integration.mcp_config.servers[server_name]
    if not server_config.enabled:
        print(json.dumps({"error": f"Server {server_name} is disabled in configuration"}, ensure_ascii=False))
        return

    client_created_for_test = False

    try:
        # Check if already connected, if not connect
        client = await _maybe_await_get_client(mcp_integration, server_name)
        if not client:
            # Get ssl_verify from centralized network config
            ssl_verify = mcp_integration.config.network.ssl_verify if mcp_integration.config and mcp_integration.config.network else True
            
            client_config = {
                "transport": server_config.transport_type,
                "url": server_config.url,
                "client_name": f"AgentSystem-{server_name}",
                "timeout": server_config.timeout,
                "ssl_verify": ssl_verify
            }

            # Add initialization options if present
            if server_config.initialization_options:
                client_config["initialization_options"] = server_config.initialization_options

            await mcp_integration.client_manager.add_client(server_name, client_config)
            client = await _maybe_await_get_client(mcp_integration, server_name)
            client_created_for_test = True

        if not client:
            print(json.dumps({"error": f"Failed to create client for {server_name}"}, ensure_ascii=False))
            return

        # Test basic functionality - list tools
        try:
            tools = await client.list_tools()
            test_result = {
                "server": server_name,
                "connection": "success",
                "tools_count": len(tools) if tools else 0,
                "tools": [tool.name for tool in tools] if tools else []
            }
        except Exception as e:
            test_result = {
                "server": server_name,
                "connection": "success",
                "tools_test": f"failed: {str(e)}"
            }

        print(json.dumps(test_result, indent=2, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"error": f"Test failed for {server_name}: {str(e)}"}, ensure_ascii=False))
    finally:
        # Clean up temporary client created for testing
        if client_created_for_test:
            try:
                await mcp_integration.client_manager.remove_client(server_name)
            except Exception as cleanup_error:
                logger.debug(f"Error cleaning up test client {server_name}: {cleanup_error}")


async def _mcp_tool_management(mcp_integration: MCPIntegration, server_name: str, args: Any) -> None:
    """Manage tools for a specific MCP server (list, allow, block)."""
    if server_name not in mcp_integration.mcp_config.servers:
        print(json.dumps({"error": f"Server {server_name} not found in configuration"}, ensure_ascii=False))
        return

    # Tool action is in the 'key' argument
    tool_action = getattr(args, 'key', None)
    if not tool_action:
        print(json.dumps({"error": "Tool action required: list, allow, or block"}, ensure_ascii=False))
        return

    if tool_action == "list":
        await _list_server_tools(mcp_integration, server_name, args)
    elif tool_action == "allow":
        tool_name = getattr(args, 'value', None)
        if not tool_name:
            print(json.dumps({"error": "Tool name required for allow action"}, ensure_ascii=False))
            return
        await _allow_server_tool(mcp_integration, server_name, tool_name)
    elif tool_action == "block":
        tool_name = getattr(args, 'value', None)
        if not tool_name:
            print(json.dumps({"error": "Tool name required for block action"}, ensure_ascii=False))
            return
        await _block_server_tool(mcp_integration, server_name, tool_name)
    else:
        print(json.dumps({"error": f"Unknown tool action: {tool_action}. Use list, allow, or block"}, ensure_ascii=False))


async def _list_server_tools(mcp_integration: MCPIntegration, server_name: str, args: Any) -> None:
    """List all available tools for a server and show filtering configuration."""
    server_config = mcp_integration.mcp_config.servers[server_name]

    # Get current tool filtering config from nested tools structure
    allowed_tools = server_config.tools.allowed if server_config.tools else None
    blocked_tools = server_config.tools.blocked if server_config.tools else None

    # Try to connect and list tools
    try:
        client = await _maybe_await_get_client(mcp_integration, server_name)
        client_created = False

        if not client and server_config.enabled:
            # Get ssl_verify from centralized network config
            ssl_verify = mcp_integration.config.network.ssl_verify if mcp_integration.config and mcp_integration.config.network else True
            
            # Create temporary client to list tools
            client_config = {
                "transport": server_config.transport_type,
                "url": server_config.url,
                "client_name": f"AgentSystem-{server_name}",
                "timeout": server_config.timeout,
                "ssl_verify": ssl_verify
            }

            if server_config.initialization_options:
                client_config["initialization_options"] = server_config.initialization_options

            await mcp_integration.client_manager.add_client(server_name, client_config)
            client = await _maybe_await_get_client(mcp_integration, server_name)
            client_created = True

        available_tools = []
        if client:
            try:
                tools = await client.list_tools()
                available_tools = [tool.name for tool in tools] if tools else []
            except Exception as e:
                print(json.dumps({"error": f"Failed to list tools: {str(e)}"}, ensure_ascii=False))
                return

        result = {
            "server": server_name,
            "available_tools": available_tools,
            "filtering": {
                "allowed_tools": allowed_tools,
                "blocked_tools": blocked_tools
            }
        }

        if allowed_tools:
            result["effective_tools"] = [t for t in available_tools if t in allowed_tools]
        elif blocked_tools:
            result["effective_tools"] = [t for t in available_tools if t not in blocked_tools]
        else:
            result["effective_tools"] = available_tools

        # Output in requested format
        if getattr(args, 'out_format', 'json') == "table":
            # Table format output
            print(f"\nServer: {server_name}")
            print("=" * (len(server_name) + 8))

            if not available_tools:
                print("No tools available")
            else:
                print(f"\nAvailable Tools ({len(available_tools)}):")
                print("-" * 30)
                for tool in available_tools:
                    status = ""
                    if blocked_tools and tool in blocked_tools:
                        status = " [BLOCKED]"
                    elif allowed_tools and tool not in allowed_tools:
                        status = " [NOT ALLOWED]"
                    print(f"  {tool}{status}")

                print(f"\nEffective Tools ({len(result['effective_tools'])}):")
                print("-" * 30)
                for tool in result["effective_tools"]:
                    print(f"  {tool}")

                if blocked_tools or allowed_tools:
                    print("\nFiltering Configuration:")
                    print("-" * 30)
                    if allowed_tools:
                        print(f"  Allowed: {', '.join(allowed_tools)}")
                    if blocked_tools:
                        print(f"  Blocked: {', '.join(blocked_tools)}")
        else:
            # JSON format output
            print(json.dumps(result, indent=2, ensure_ascii=False))

        # Clean up temporary client
        if client_created:
            try:
                await mcp_integration.client_manager.remove_client(server_name)
            except Exception as cleanup_error:
                logger.debug(f"Error cleaning up tool list client {server_name}: {cleanup_error}")

    except Exception as e:
        print(json.dumps({"error": f"Failed to list tools for {server_name}: {str(e)}"}, ensure_ascii=False))


async def _allow_server_tool(mcp_integration: MCPIntegration, server_name: str, tool_name: str) -> None:
    """Add a tool to the allowed_tools list for a server."""
    cfg_path = Path("config/mcp.yaml")
    if not cfg_path.exists():
        print(json.dumps({"error": f"Configuration file {cfg_path} not found"}, ensure_ascii=False))
        return

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(json.dumps({"error": f"Failed to read config: {str(e)}"}, ensure_ascii=False))
        return

    mcp_block = raw.get("mcp", raw)
    servers = mcp_block.get("external_servers", {})
    if server_name not in servers:
        print(json.dumps({"error": f"Server {server_name} not found in config"}, ensure_ascii=False))
        return

    server_cfg = servers[server_name] or {}
    allowed = list(server_cfg.get("allowed_tools") or [])
    blocked = list(server_cfg.get("blocked_tools") or [])

    if tool_name in allowed:
        print(json.dumps({"message": "Tool already allowed", "server": server_name, "tool": tool_name}, ensure_ascii=False))
        return

    # Ensure tool is not in blocked list
    if tool_name in blocked:
        blocked.remove(tool_name)
        server_cfg["blocked_tools"] = blocked

    allowed.append(tool_name)
    server_cfg["allowed_tools"] = allowed
    servers[server_name] = server_cfg
    mcp_block["external_servers"] = servers
    # Put back into top-level structure if original used mcp key
    if "mcp" in raw:
        raw["mcp"] = mcp_block
    else:
        raw = mcp_block

    try:
        data = yaml.safe_dump(raw, sort_keys=False)
        _atomic_write_text(cfg_path, data)
        print(json.dumps({"message": "allowed_tools updated", "server": server_name, "tool": tool_name}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"Failed to write config: {str(e)}"}, ensure_ascii=False))


async def _block_server_tool(mcp_integration: MCPIntegration, server_name: str, tool_name: str) -> None:
    """Add a tool to the blocked_tools list for a server."""
    cfg_path = Path("config/mcp.yaml")
    if not cfg_path.exists():
        print(json.dumps({"error": f"Configuration file {cfg_path} not found"}, ensure_ascii=False))
        return

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(json.dumps({"error": f"Failed to read config: {str(e)}"}, ensure_ascii=False))
        return

    mcp_block = raw.get("mcp", raw)
    servers = mcp_block.get("external_servers", {})
    if server_name not in servers:
        print(json.dumps({"error": f"Server {server_name} not found in config"}, ensure_ascii=False))
        return

    server_cfg = servers[server_name] or {}
    allowed = list(server_cfg.get("allowed_tools") or [])
    blocked = list(server_cfg.get("blocked_tools") or [])

    if tool_name in blocked:
        print(json.dumps({"message": "Tool already blocked", "server": server_name, "tool": tool_name}, ensure_ascii=False))
        return

    # Ensure tool is not in allowed list
    if tool_name in allowed:
        allowed.remove(tool_name)
        server_cfg["allowed_tools"] = allowed

    blocked.append(tool_name)
    server_cfg["blocked_tools"] = blocked
    servers[server_name] = server_cfg
    mcp_block["external_servers"] = servers
    if "mcp" in raw:
        raw["mcp"] = mcp_block
    else:
        raw = mcp_block

    try:
        data = yaml.safe_dump(raw, sort_keys=False)
        _atomic_write_text(cfg_path, data)
        print(json.dumps({"message": "blocked_tools updated", "server": server_name, "tool": tool_name}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"Failed to write config: {str(e)}"}, ensure_ascii=False))


async def handle_mcp_command(mcp_integration: MCPIntegration, args: Any) -> None:
    """Handle MCP subcommands."""
    action = getattr(args, 'action', None)
    server = getattr(args, 'server', None)

    if action == "list":
        await _mcp_list_servers(mcp_integration, args)
    elif action == "connect":
        if not server:
            print(json.dumps({"error": "Server name required for connect action"}, ensure_ascii=False))
            return
        await _mcp_connect_server(mcp_integration, server, args)
    elif action == "disconnect":
        if not server:
            print(json.dumps({"error": "Server name required for disconnect action"}, ensure_ascii=False))
            return
        await _mcp_disconnect_server(mcp_integration, server, args)
    elif action == "status":
        await _mcp_status_servers(mcp_integration, server, args)
    elif action == "test":
        if not server:
            print(json.dumps({"error": "Server name required for test action"}, ensure_ascii=False))
            return
        await _mcp_test_server(mcp_integration, server, args)
    elif action == "tool":
        if not server:
            print(json.dumps({"error": "Server name required for tool action"}, ensure_ascii=False))
            return
        await _mcp_tool_management(mcp_integration, server, args)
    elif action == "enable":
        if not server:
            print(json.dumps({"error": "Server name required for enable action"}, ensure_ascii=False))
            return
        # Enable server logic would go here
        print(json.dumps({"error": "Enable action not yet implemented"}, ensure_ascii=False))
    elif action == "disable":
        if not server:
            print(json.dumps({"error": "Server name required for disable action"}, ensure_ascii=False))
            return
        # Disable server logic would go here
        print(json.dumps({"error": "Disable action not yet implemented"}, ensure_ascii=False))
    else:
        print(json.dumps({"error": f"Unknown MCP action: {action}"}, ensure_ascii=False))