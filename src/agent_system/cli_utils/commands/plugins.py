"""`agent-cli plugins`: the discovered plugins -- list, info, search.

Read-only and cheap: only ``discover_all_plugins`` over ``plugins.plugin_dirs``
runs, nothing is started. What counts as enabled is read raw from
``plugins.servers``, as ``ToolServerIntegration`` does when it registers: a
plugin is a TYPE, its instances are what plugins.yaml enables, and the type
follows the ``type:`` chain down to the plugin.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ...plugins import discover_all_plugins
from ..common import colorize, supports_color
from .table import print_table


def _plugin_type_of(servers: Dict[str, Any], plugins: Dict[str, Any], instance_name: str) -> str:
    """The plugin an instance is built from. `type:` may name another
    server, whose own type then counts -- the chain
    settings._resolve_server_inheritance follows. Read raw, a child
    of `base: {type: web_scraper}` was no web_scraper at all."""
    typ = servers[instance_name].type
    seen = {instance_name}
    while typ not in plugins and typ in servers and typ not in seen:
        seen.add(typ)
        typ = servers[typ].type
    return typ


def _plugin_listing(servers: Dict[str, Any], plugins: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a list of plugins with their instances grouped by type."""
    out = []
    # Build a mapping of plugin_type -> list of instances
    type_to_instances: Dict[str, List[Dict[str, Any]]] = {}

    for instance_name, server_config in servers.items():
        plugin_type = _plugin_type_of(servers, plugins, instance_name)
        if plugin_type not in type_to_instances:
            type_to_instances[plugin_type] = []
        type_to_instances[plugin_type].append({
            "instance_name": instance_name,
            "enabled": server_config.enabled,
            "description": server_config.description or "",
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


def _enabled_cell(enabled: Any) -> str:
    """YES or NO, green or red where colours show."""
    text = "YES" if enabled else "NO"
    if supports_color():
        return colorize(text, "32" if enabled else "31")
    return text


def _clipped(text: str) -> str:
    """A description cut to the table's 80 characters."""
    return text[:77] + "..." if len(text) > 80 else text


def _print_plugin_info(args: Any, servers: Dict[str, Any], plugins: Dict[str, Any]) -> None:
    """info action: print metadata for a specific plugin"""
    target = getattr(args, "name", None)
    if not target:
        print(json.dumps({"error": "missing plugin name"}, ensure_ascii=False))
        sys.exit(1)
    factory = plugins.get(target)
    if not factory:
        print(json.dumps({"error": "plugin not found", "name": target}, ensure_ascii=False))
        sys.exit(1)
    meta = getattr(factory, "_plugin_metadata", None) or {}
    # A plugin is a TYPE; what plugins.yaml enables are its instances,
    # whose names need not match the type (writer_audio_ops is an
    # audio_ops). Same rule as the listing: enabled when any is.
    enabled_flag = any(
        v.enabled for k, v in servers.items() if _plugin_type_of(servers, plugins, k) == target)
    if args.raw:
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
        out = {"name": target, "metadata": meta, **factory_info, "enabled": enabled_flag}
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
        print(f"ENABLED: {_enabled_cell(enabled_flag)}")
        return
    print(json.dumps({"name": target, "metadata": meta, "enabled": enabled_flag}, indent=2, ensure_ascii=False))


def _print_plugin_table(listing: List[Dict[str, Any]]) -> None:
    """nice table layout using tabulate if available"""
    rows: List[Tuple[str, str, str, str]] = []
    for p in listing:
        # Add the main plugin type row with truncated description
        desc = _clipped(p.get("description") or "")
        rows.append((p.get("name") or "", _enabled_cell(bool(p.get("enabled"))), desc, p.get("version") or ""))

        # Add instance rows if multiple instances exist
        instances = p.get("instances", [])
        if instances:
            for inst in instances:
                # Indent instance name with tree characters
                inst_name = f"  ├─ {inst.get('instance_name', '')}"
                inst_desc = _clipped(inst.get("description", ""))
                rows.append((inst_name, _enabled_cell(inst.get("enabled", False)), inst_desc, ""))

    print_table(rows, ["NAME", "ENABLED", "DESCRIPTION", "VERSION"], pad_last=False)


def run_plugins_command(args: Any, config: Any) -> None:
    """List, describe or search the discovered plugins."""
    # Use the configured plugin_dirs from the loaded settings. The
    # `load_settings()` call resolves relative paths against the
    # config file directory, so we can trust these paths as provided by
    # the user. If no plugin dirs are configured, pass None to
    # `discover_all_plugins()` to discover only entrypoint plugins.
    plugins_cfg = config.plugins
    dirs = [Path(p) for p in (plugins_cfg.plugin_dirs or []) if p] if plugins_cfg else []
    plugins = discover_all_plugins(dirs if dirs else None)

    servers = plugins_cfg.servers if plugins_cfg else {}

    if getattr(args, "action", None) == "info":
        _print_plugin_info(args, servers, plugins)
        return

    # search action: filter plugins by name or description
    if getattr(args, "action", None) == "search":
        term = (getattr(args, "name", None) or "").lower()
        listing = _plugin_listing(servers, plugins)
        filtered = [p for p in listing if term in (p["name"] or "").lower() or term in (p.get("description") or "").lower()]
        print(json.dumps(filtered, indent=2, ensure_ascii=False))
        return

    # list action: either json or simple table
    listing = _plugin_listing(servers, plugins)
    # If user requested metadata in the listing and JSON format, attach it
    if getattr(args, "show_metadata", False) and args.out_format == "json":
        for item in listing:
            factory = plugins.get(item["name"])
            item["metadata"] = getattr(factory, "_plugin_metadata", None) or {}

    # Metadata should have been attached by discover_all_plugins() when
    # filesystem plugin dirs were provided. If any metadata is still
    # missing, leave it blank rather than attempting to read repository
    # paths — callers should configure plugin_dirs in `mcp.yaml` if
    # they expect filesystem plugin metadata to be used.
    if args.out_format == "table":
        _print_plugin_table(listing)
        return

    print(json.dumps(listing, indent=2, ensure_ascii=False))
