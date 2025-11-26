"""Plugin management commands for the CLI."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

logger = logging.getLogger(__name__)


def _plugin_list(plugins: dict, config: Any, args: Any) -> None:
    """List all available plugins and their status."""
    if args.out_format == "json":
        out = []
        for name, factory in plugins.items():
            meta = getattr(factory, "_plugin_metadata", None) or {}
            # include whether this plugin is enabled in the current config
            enabled_set = set((config.mcp.enabled_servers or []) or [])
            enabled_flag = name in enabled_set
            out.append({
                "name": name,
                "description": meta.get("description"),
                "version": meta.get("version"),
                "enabled": enabled_flag,
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        # Table format
        if not plugins:
            print("No plugins found.")
            return

        rows = []
        for name, factory in plugins.items():
            meta = getattr(factory, "_plugin_metadata", None) or {}
            enabled_set = set((config.mcp.enabled_servers or []) or [])
            enabled_flag = name in enabled_set
            status = "Enabled" if enabled_flag else "Disabled"
            rows.append((name, meta.get("version", ""), status, meta.get("description", "")))

        headers = ["NAME", "VERSION", "STATUS", "DESCRIPTION"]
        if tabulate:
            print(tabulate(rows, headers=headers, tablefmt="github"))
        else:
            # Simple fallback
            if rows:
                name_w = max(len(str(r[0])) for r in rows)
                ver_w = max(len(str(r[1])) for r in rows)
                status_w = max(len(str(r[2])) for r in rows)
                desc_w = max(len(str(r[3])) for r in rows)
            else:
                name_w = ver_w = status_w = desc_w = 10
            hdr = f"{'NAME'.ljust(name_w)}  {'VERSION'.ljust(ver_w)}  {'STATUS'.ljust(status_w)}  {'DESCRIPTION'.ljust(desc_w)}"
            print(hdr)
            print("-" * len(hdr))
            for n, v, s, d in rows:
                print(f"{str(n).ljust(name_w)}  {str(v).ljust(ver_w)}  {str(s).ljust(status_w)}  {str(d).ljust(desc_w)}")


def _plugin_info(plugins: dict, config: Any, plugin_name: str, args: Any) -> None:
    """Show detailed information about a plugin."""
    factory = plugins.get(plugin_name)
    if not factory:
        print(json.dumps({"error": "plugin not found", "name": plugin_name}, ensure_ascii=False))
        return

    meta = getattr(factory, "_plugin_metadata", None) or {}
    enabled_set = set((config.mcp.enabled_servers or []) or [])
    enabled_flag = plugin_name in enabled_set

    # raw output: include factory repr and module path
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
        out = {"name": plugin_name, "metadata": meta, **factory_info, "enabled": enabled_flag}
        if args.out_format == "table":
            # Print header and key/value lines
            print(f"NAME: {plugin_name}")
            for k, v in out.items():
                if k == "name":
                    continue
                print(f"{k.upper()}: {v}")
            return
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    # human table optionally
    if args.out_format == "table":
        print(f"NAME: {plugin_name}")
        print(f"ENABLED: {enabled_flag}")
        print(f"VERSION: {meta.get('version', 'unknown')}")
        print(f"DESCRIPTION: {meta.get('description', 'no description')}")
        if meta:
            print("METADATA:")
            for k, v in meta.items():
                print(f"  {k}: {v}")
    else:
        out = {
            "name": plugin_name,
            "enabled": enabled_flag,
            "metadata": meta
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))


def _plugin_enable(plugins: dict, config: Any, plugin_name: str, args: Any) -> None:
    """Enable a plugin (not yet implemented - would modify config)."""
    if plugin_name not in plugins:
        print(json.dumps({"error": f"Plugin {plugin_name} not found"}, ensure_ascii=False))
        return

    print(json.dumps({"error": "Plugin enable/disable not yet implemented - modify config.mcp.enabled_servers manually"}, ensure_ascii=False))


def _plugin_disable(plugins: dict, config: Any, plugin_name: str, args: Any) -> None:
    """Disable a plugin (not yet implemented - would modify config)."""
    if plugin_name not in plugins:
        print(json.dumps({"error": f"Plugin {plugin_name} not found"}, ensure_ascii=False))
        return

    print(json.dumps({"error": "Plugin enable/disable not yet implemented - modify config.mcp.enabled_servers manually"}, ensure_ascii=False))


async def _plugin_status(plugins: dict, config: Any, plugin_name: str | None, args: Any) -> None:
    """Show status of plugins."""
    if plugin_name:
        _plugin_info(plugins, config, plugin_name, args)
    else:
        _plugin_list(plugins, config, args)


async def handle_plugin_command(plugins: dict, config: Any, args: Any) -> None:
    """Handle plugin subcommands."""
    action = getattr(args, 'action', 'list')

    if action == "list":
        _plugin_list(plugins, config, args)
    elif action == "info":
        plugin_name = getattr(args, 'name', None)
        if not plugin_name:
            print(json.dumps({"error": "Plugin name required for info action"}, ensure_ascii=False))
            return
        _plugin_info(plugins, config, plugin_name, args)
    elif action == "enable":
        plugin_name = getattr(args, 'name', None)
        if not plugin_name:
            print(json.dumps({"error": "Plugin name required for enable action"}, ensure_ascii=False))
            return
        _plugin_enable(plugins, config, plugin_name, args)
    elif action == "disable":
        plugin_name = getattr(args, 'name', None)
        if not plugin_name:
            print(json.dumps({"error": "Plugin name required for disable action"}, ensure_ascii=False))
            return
        _plugin_disable(plugins, config, plugin_name, args)
    elif action == "status":
        plugin_name = getattr(args, 'name', None)
        await _plugin_status(plugins, config, plugin_name, args)
    elif action == "search":
        # Search functionality not implemented yet
        print(json.dumps({"error": "Plugin search not yet implemented"}, ensure_ascii=False))
    else:
        print(json.dumps({"error": f"Unknown plugin action: {action}"}, ensure_ascii=False))