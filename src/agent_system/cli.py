from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml
from typing import Any, Dict, List, Tuple

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

from .config.settings import load_settings
from .plugins import discover_all_plugins
from .mcp.base import MCPRegistry
from .mcp.status import status_bus
from .mcp.integration import MCPIntegration
from .utils.logging import setup_logging
from .servers.bootstrap import bootstrap_servers
from .servers.agent.server import Agent

# Global color mode: tests may monkeypatch this variable
color_mode: str = "auto"

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, data: str) -> None:
    """Atomically write text to `path` by writing to a temp file in the
    same directory and renaming it into place. Ensures durable write where
    possible by flushing and syncing file content and directory."""
    dirpath = path.parent
    fd, tmp = tempfile.mkstemp(dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                # Some platforms or filesystems may not support fsync; ignore
                pass
        try:
            os.replace(tmp, str(path))
        except Exception:
            # On some systems os.replace requires str paths
            os.replace(tmp, path)
        # Attempt to sync directory metadata
        try:
            # os.O_DIRECTORY is not available on all platforms (notably
            # Windows). Only attempt to open and fsync the directory when
            # the flag exists; otherwise skip directory fsync.
            if hasattr(os, "O_DIRECTORY"):
                dirfd = os.open(dirpath, os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
        except Exception:
            pass
    finally:
        # Clean up tmp if still exists
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


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
            client_config = {
                "transport": server_config.transport_type,
                "url": server_config.url,
                "client_name": f"AgentSystem-{server_name}",
                "timeout": server_config.timeout,
                "ssl_verify": server_config.ssl_verify
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
    
    # Get current tool filtering config
    allowed_tools = getattr(server_config, 'allowed_tools', None)
    blocked_tools = getattr(server_config, 'blocked_tools', None)
    
    # Try to connect and list tools
    try:
        client = await _maybe_await_get_client(mcp_integration, server_name)
        client_created = False
        
        if not client and server_config.enabled:
            # Create temporary client to list tools
            client_config = {
                "transport": server_config.transport_type,
                "url": server_config.url,
                "client_name": f"AgentSystem-{server_name}",
                "timeout": server_config.timeout,
                "ssl_verify": server_config.ssl_verify
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



def main() -> None:
    # Backward-compatible: allow calling `agent-cli <task>` without an explicit subcommand.
    # If the first non-option arg isn't a known subcommand, inject an implicit 'run' subcommand.
    # Use a two-stage parse: first extract global options from anywhere using parse_known_args,
    # then parse the remaining args (subcommand + subargs). This avoids confusing option values
    # with subcommands when we need to insert an implicit 'run'.
    prelim = argparse.ArgumentParser(add_help=False)
    prelim.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")))
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
    known = ("plugins", "mcp", "run", "status", "-h", "--help")
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
    parser.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")), help="Path to config")
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
        except Exception:
            pass
        try:
            p.add_argument("--timeout", dest="timeout", type=int, default=30, help="Timeout in seconds for connection operations")
        except Exception:
            pass
        try:
            p.add_argument("--no-probe", dest="no_probe", action="store_true", help="When listing features, don't probe the live server for reported capabilities; only show configured values")
        except Exception:
            pass

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
        dirs = [Path(p) for p in (config.mcp.plugin_dirs or []) if p]
        plugins = discover_all_plugins(dirs if dirs else None)

        def to_list():
            out = []
            for name, factory in plugins.items():
                meta = getattr(factory, "_plugin_metadata", None) or {}
                # metadata loading is handled centrally in discover_all_plugins();
                # keep local code minimal.
                # include whether this plugin is enabled in the current config
                enabled_set = set((config.mcp.enabled_servers or []) or [])
                enabled_flag = name in enabled_set
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
                out["enabled"] = target in set((config.mcp.enabled_servers or []) or [])
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
                enabled_flag = target in set((config.mcp.enabled_servers or []) or [])
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

        # enable/disable actions: persist to the YAML config's mcp.enabled_servers
        if getattr(args, "action", None) in ("enable", "disable"):
            target = getattr(args, "name", None)
            if not target:
                print(json.dumps({"error": "missing plugin name"}, ensure_ascii=False))
                return
            cfg_path = Path(args.config)
            # Read master manifest to discover included files and prefer writing
            # to the included file that contains an `mcp` mapping (e.g. `mcp.yaml`).
            managed_path = cfg_path
            try:
                master_raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                master_raw = {}

            includes = master_raw.get("includes") or master_raw.get("files") or []
            if isinstance(includes, str):
                includes = [includes]

            # Attempt to find an included file that contains 'mcp' mapping
            found = None
            for inc in includes:
                inc_path = Path(inc)
                if not inc_path.is_absolute():
                    inc_path = cfg_path.parent.joinpath(inc_path)
                if inc_path.exists():
                    try:
                        inc_data = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
                        if isinstance(inc_data, dict) and "mcp" in inc_data:
                            found = inc_path
                            break
                    except Exception:
                        continue

            # If a specific mcp-managed file was found in includes, use it.
            if found:
                managed_path = found
            else:
                # Fallback: if master declares mcp.managed_file, respect it; otherwise
                # default to writing a `.managed` sibling next to master (legacy behavior)
                mcp_master = master_raw.get("mcp", {}) or {}
                mf = mcp_master.get("managed_file")
                if mf:
                    mp = Path(mf)
                    if not mp.is_absolute():
                        managed_path = cfg_path.parent.joinpath(mp)
                    else:
                        managed_path = mp
                else:
                    managed_path = cfg_path.with_name(cfg_path.stem + ".managed" + cfg_path.suffix)

            # load managed data (this is what we'll update)
            managed_data: Dict[str, Any] = {}
            if managed_path.exists():
                try:
                    managed_data = yaml.safe_load(managed_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    managed_data = {}
            mcp = managed_data.get("mcp", {}) or {}
            enabled = set(mcp.get("enabled_servers", []) or [])

            intended = "enable" if args.action == "enable" else "disable"
            # preview/dry-run
            if args.dry_run:
                preview = sorted(enabled | {target}) if intended == "enable" else sorted(enabled - {target})
                print(json.dumps({"dry_run": True, "action": intended, "preview_enabled": preview}, ensure_ascii=False))
                return

            # confirm unless --yes. If stdin or stdout are not a TTY (non-interactive/test), skip prompt.
            if not args.yes and (sys.stdin.isatty() and sys.stdout.isatty()):
                resp = input(f"Are you sure you want to {intended} plugin '{target}'? [y/N]: ")
                if resp.strip().lower() not in ("y", "yes"):
                    print(json.dumps({"result": "cancelled"}, ensure_ascii=False))
                    return

            if args.action == "enable":
                enabled.add(target)
            else:
                enabled.discard(target)
            mcp["enabled_servers"] = sorted(enabled)
            # persist into the managed_data (not the master data) so we don't overwrite
            # user-edited master config. We'll write managed_data to managed_path.
            managed_data["mcp"] = mcp
            # atomic write only: write temp file in same dir and atomically replace target.
            try:
                # Write managed data as YAML (not JSON) to preserve expected
                # config formatting and allow editing by users. Keep atomic
                # replace semantics: write to a temp file in the same dir and
                # then atomically replace the target.
                with tempfile.NamedTemporaryFile("w", delete=False, dir=str(managed_path.parent), encoding="utf-8") as tf:
                    # Use safe_dump with sort_keys=False to preserve order where possible
                    yaml.safe_dump(managed_data, tf, allow_unicode=True, sort_keys=False)
                    tmp_name = tf.name
                os.replace(tmp_name, str(managed_path))
            except Exception as e:
                print(json.dumps({"error": "failed to write config", "reason": str(e)}, ensure_ascii=False))
                # cleanup temp file if present
                try:
                    if 'tmp_name' in locals() and os.path.exists(tmp_name):
                        os.remove(tmp_name)
                except Exception:
                    pass
                return
            print(json.dumps({"result": "ok", "enabled": mcp["enabled_servers"]}, ensure_ascii=False))
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
            # load config file to read enabled list
            cfg_path = Path(args.config)
            data: Dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    data = {}
            enabled = set((data.get("mcp") or {}).get("enabled_servers", []) or [])
            listing = to_list()
            for p in listing:
                p["enabled"] = p["name"] in enabled
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
        # Load MCP configuration and create integration
        # Prefer reading the included managed `config/mcp.yaml` so external_servers
        # entries are preserved and visible to MCPIntegration (AgentConfig.mcp
        # may be a pydantic model that doesn't keep the raw 'external_servers' block).
        config_path = getattr(config.mcp, 'config_file', None) or "config/mcp.yaml"

        async def handle_mcp_command():
            # Read the managed MCP file directly if present, otherwise fall back
            # to the pydantic model dump. This ensures `external_servers` is
            # available to MCPIntegration.load_config which expects an
            # `mcp.external_servers` mapping.
            try:
                if isinstance(config_path, str):
                    mcp_file = Path(config_path)
                else:
                    mcp_file = Path(str(config_path))
                if not mcp_file.is_absolute():
                    mcp_file = Path("config") / mcp_file.name
                if mcp_file.exists():
                    try:
                        raw = yaml.safe_load(mcp_file.read_text(encoding="utf-8")) or {}
                        mcp_block = raw.get("mcp", raw)
                    except Exception:
                        mcp_block = (config.mcp.model_dump() if hasattr(config.mcp, "model_dump") else getattr(config.mcp, "__dict__", {}))
                else:
                    mcp_block = (config.mcp.model_dump() if hasattr(config.mcp, "model_dump") else getattr(config.mcp, "__dict__", {}))
            except Exception:
                mcp_block = (config.mcp.model_dump() if hasattr(config.mcp, "model_dump") else getattr(config.mcp, "__dict__", {}))

            mcp_integration = MCPIntegration(config={"mcp": mcp_block})
            # Ensure MCPIntegration sets up external clients and plugins
            try:
                try:
                    await mcp_integration.initialize({"mcp": mcp_block})
                except Exception:
                    # Non-fatal: continue without live clients if initialization fails
                    pass

                action = getattr(args, "action", "list")
                server_name = getattr(args, "server", None)
                if action == "list":
                    result = await _mcp_list_servers(mcp_integration, args)
                    return
                elif action in ("enable", "disable"):
                    if not server_name:
                        print(json.dumps({"error": "server name required for enable/disable action"}, ensure_ascii=False))
                        return
                    # Find the managed mcp file path same way earlier: try to resolve config.mcp.config_file if present
                    cfg_path = Path(config.mcp.config_file) if getattr(config.mcp, 'config_file', None) else Path("config/mcp.yaml")
                    if not cfg_path.is_absolute():
                        cfg_path = Path("config") / cfg_path.name

                    managed_data = {}
                    if cfg_path.exists():
                        try:
                            managed_data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                        except Exception:
                            managed_data = {}

                    mcp_block = managed_data.get("mcp", {}) or {}
                    external = mcp_block.get("external_servers", {}) or {}
                    if server_name not in external:
                        print(json.dumps({"error": f"Server {server_name} not found in managed config"}, ensure_ascii=False))
                        return

                    intended = action

                    # perform update (always persist)
                    external[server_name] = dict(external[server_name])
                    external[server_name]["enabled"] = True if intended == "enable" else False
                    mcp_block["external_servers"] = external
                    managed_data["mcp"] = mcp_block

                    # atomic write to cfg_path
                    try:
                        with tempfile.NamedTemporaryFile("w", delete=False, dir=str(cfg_path.parent), encoding="utf-8") as tf:
                            yaml.safe_dump(managed_data, tf, allow_unicode=True, sort_keys=False)
                            tmp_name = tf.name
                        os.replace(tmp_name, str(cfg_path))
                    except Exception as e:
                        print(json.dumps({"error": "failed to write managed config", "reason": str(e)}, ensure_ascii=False))
                        try:
                            if 'tmp_name' in locals() and os.path.exists(tmp_name):
                                os.remove(tmp_name)
                        except Exception:
                            pass
                        return

                    print(json.dumps({"result": "ok", "server": server_name, "enabled": external[server_name]["enabled"]}, ensure_ascii=False))
                    result = None
                    return
                elif action == "feature":
                    # key is subaction 'list' or 'set'; if key is 'list', show capabilities
                    sub = getattr(args, "key", None)
                    if not server_name:
                        print(json.dumps({"error": "server name required for feature action"}, ensure_ascii=False))
                        return

                    # Read managed config file
                    cfg_path = Path(config.mcp.config_file) if getattr(config.mcp, 'config_file', None) else Path("config/mcp.yaml")
                    if not cfg_path.is_absolute():
                        cfg_path = Path("config") / cfg_path.name
                    managed_data = {}
                    if cfg_path.exists():
                        try:
                            managed_data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                        except Exception:
                            managed_data = {}

                    mcp_block = managed_data.get("mcp", {}) or {}
                    external = mcp_block.get("external_servers", {}) or {}
                    if server_name not in external:
                        print(json.dumps({"error": f"Server {server_name} not found in managed config"}, ensure_ascii=False))
                        return

                    # LIST features: query server capabilities via client if connected; otherwise show config features
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
                                    except Exception:
                                        capabilities = None
                            except Exception:
                                capabilities = None

                        conf_features = external[server_name].get('features', {})
                        out = {
                            'server': server_name,
                            'configured_features': conf_features,
                            'reported_capabilities': capabilities
                        }
                        print(json.dumps(out, indent=2, ensure_ascii=False))
                        result = None
                        return

                    # SET feature: args.key is feature name, args.value is on/off
                    feature_name = sub
                    feature_val = getattr(args, 'value', None)
                    if not feature_name or feature_val not in ("on", "off"):
                        print(json.dumps({"error": "Usage: agent-cli mcp feature <server> <feature> <on|off>"}, ensure_ascii=False))
                        return

                    enabled_val = True if feature_val == "on" else False
                    # Persist to managed config
                    external[server_name] = dict(external[server_name])
                    features = dict(external[server_name].get('features', {}) or {})
                    features[feature_name] = enabled_val
                    external[server_name]['features'] = features
                    mcp_block['external_servers'] = external
                    managed_data['mcp'] = mcp_block

                    try:
                        with tempfile.NamedTemporaryFile("w", delete=False, dir=str(cfg_path.parent), encoding="utf-8") as tf:
                            yaml.safe_dump(managed_data, tf, allow_unicode=True, sort_keys=False)
                            tmp_name = tf.name
                        os.replace(tmp_name, str(cfg_path))
                    except Exception as e:
                        print(json.dumps({"error": "failed to write managed config", "reason": str(e)}, ensure_ascii=False))
                        try:
                            if 'tmp_name' in locals() and os.path.exists(tmp_name):
                                os.remove(tmp_name)
                        except Exception:
                            pass
                        return

                    print(json.dumps({"result": "ok", "server": server_name, "feature": feature_name, "enabled": enabled_val}, ensure_ascii=False))
                    result = None
                    return
                elif action == "connect":
                    if not server_name:
                        print(json.dumps({"error": "server name required for connect action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_connect_server(mcp_integration, server_name, args)
                    return
                elif action == "disconnect":
                    if not server_name:
                        print(json.dumps({"error": "server name required for disconnect action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_disconnect_server(mcp_integration, server_name, args)
                    return
                elif action == "status":
                    result = await _mcp_status_servers(mcp_integration, server_name, args)
                    return
                elif action == "test":
                    if not server_name:
                        print(json.dumps({"error": "server name required for test action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_test_server(mcp_integration, server_name, args)
                    return
                elif action == "tool":
                    if not server_name:
                        print(json.dumps({"error": "server name required for tool action"}, ensure_ascii=False))
                        result = None
                        return
                    result = await _mcp_tool_management(mcp_integration, server_name, args)
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
        except Exception:
            # fallback to a simple role-specific name in logs/
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
    agent = Agent("cli_agent", config, registry=registry)

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)
    # Stream execution and show MCP call/results on the fly in a human readable way.
    # Modern execution with status events and optional MCP call display
    async def _stream_and_run_with_status(agent: Agent, task: str, show_mcp: bool = False, show_status: bool = True) -> dict:
        final_result: Dict[str, Any] = {"task": task, "calls": []}

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
                    # Display status event in a clean format
                    phase = getattr(event, "phase", "progress")
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

                    # Level coloring overrides overall line if error/warning
                    if event.level == "error" and _supports_color():
                        status_line = _colorize(status_line, "31")
                    elif event.level == "warning" and _supports_color():
                        status_line = _colorize(status_line, "33")
                    print(status_line)
            except asyncio.CancelledError:
                return
            except Exception:
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
                                except Exception:
                                    obj = {"raw": payload}
                                # Print SSE messages in short form
                                if _supports_color():
                                    print(_colorize(f"[SSE] {obj.get('server','?')}: {obj.get('message','')}", "34"))
                                else:
                                    print(f"[SSE] {obj.get('server','?')}: {obj.get('message','')}")
            except asyncio.CancelledError:
                return
            except Exception:
                return

        # Start status subscriber task if enabled
        status_task = None
        if show_status and status_queue:
            status_task = asyncio.create_task(_status_subscriber())

        if sse_url:
            try:
                sse_task = asyncio.create_task(_sse_subscriber(sse_url))
            except Exception:
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
                        except Exception:
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
            if status_task and not status_task.done():
                try:
                    status_task.cancel()
                except Exception:
                    pass
            if sse_task and not sse_task.done():
                try:
                    sse_task.cancel()
                except Exception:
                    pass

    # Execute with new status-aware streaming
    show_mcp = getattr(args, "show_mcp", False)
    show_status = not getattr(args, "no_status", False)

    if getattr(args, "raw", False):
        # Raw mode: use blocking run and print JSON only
        result = asyncio.run(agent.run(args.task))
    else:
        result = asyncio.run(_stream_and_run_with_status(agent, args.task, show_mcp=show_mcp, show_status=show_status))
    vprint("[cli] done")
    logger.info("Task completed")

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
                except Exception:
                    # Fallback to JSON-ish string
                    try:
                        j = json.dumps(result_obj, ensure_ascii=False)
                        print(f"    {j}")
                    except Exception:
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
