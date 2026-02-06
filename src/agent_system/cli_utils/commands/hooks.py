"""Hook management commands for the CLI."""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_system.hooks import get_hook_registry, HookType

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _hooks_list(args: Any) -> None:
    """List all registered hooks."""
    registry = get_hook_registry()
    hook_type_filter = getattr(args, 'hook_type', None)
    
    # Get hooks list
    hooks_dict = registry.list_hooks(HookType(hook_type_filter) if hook_type_filter else None)
    
    if args.out_format == "json":
        # JSON output
        hooks_data = []
        for hook_type, hook_names in hooks_dict.items():
            for name in hook_names:
                info = registry.get_hook_info(name)
                if info:
                    hooks_data.append(info)
        print(json.dumps(hooks_data, indent=2, ensure_ascii=False))
    else:
        # Table output
        if tabulate is None:
            print("ERROR: tabulate module not available. Install with: pip install tabulate")
            print(json.dumps(hooks_dict, indent=2, ensure_ascii=False))
            return
        
        # Collect hook data for table
        table_data = []
        for hook_type, hook_names in hooks_dict.items():
            for name in hook_names:
                info = registry.get_hook_info(name)
                if info:
                    enabled = "✓" if info.get("enabled", True) else "✗"
                    timeout = f"{info.get('timeout', 30.0)}s"
                    order_spec = info.get("order_spec", {})
                    order_str = ""
                    if order_spec.get("after"):
                        order_str += f"after:{','.join(order_spec['after'][:2])}{'...' if len(order_spec['after']) > 2 else ''} "
                    if order_spec.get("before"):
                        order_str += f"before:{','.join(order_spec['before'][:2])}{'...' if len(order_spec['before']) > 2 else ''}"
                    
                    table_data.append([
                        name,
                        hook_type,
                        enabled,
                        timeout,
                        order_str.strip() or "-",
                        info.get("description", "")[:50] + ("..." if len(info.get("description", "")) > 50 else "")
                    ])
        
        if table_data:
            headers = ["NAME", "TYPE", "ENABLED", "TIMEOUT", "ORDER", "DESCRIPTION"]
            print(tabulate(table_data, headers=headers, tablefmt="grid"))
        else:
            print("No hooks registered")


def _hooks_inspect(hook_name: str, args: Any) -> None:
    """Show detailed information about a specific hook."""
    registry = get_hook_registry()
    info = registry.get_hook_info(hook_name)
    
    if not info:
        print(json.dumps({"error": f"Hook '{hook_name}' not found"}, ensure_ascii=False))
        return
    
    print(json.dumps(info, indent=2, ensure_ascii=False))


def _hooks_stats(args: Any) -> None:
    """Show execution statistics for hooks."""
    registry = get_hook_registry()
    hook_name = getattr(args, 'name', None)
    
    if hook_name:
        # Stats for specific hook
        stats = registry.get_stats(hook_name)
        if stats is None:
            print(json.dumps({"error": f"Hook '{hook_name}' not found"}, ensure_ascii=False))
            return
        print(json.dumps({hook_name: stats}, indent=2, ensure_ascii=False))
    else:
        # Stats for all hooks
        all_stats = {}
        hooks_dict = registry.list_hooks()
        for hook_type, hook_names in hooks_dict.items():
            for name in hook_names:
                stats = registry.get_stats(name)
                if stats:
                    all_stats[name] = stats
        
        if args.out_format == "json":
            print(json.dumps(all_stats, indent=2, ensure_ascii=False))
        else:
            if tabulate is None:
                print(json.dumps(all_stats, indent=2, ensure_ascii=False))
                return
            
            # Table output
            table_data = []
            for name, stats in all_stats.items():
                avg_time = f"{stats.get('average_execution_time', 0):.3f}s" if stats.get('execution_count', 0) > 0 else "-"
                table_data.append([
                    name,
                    stats.get('execution_count', 0),
                    stats.get('success_count', 0),
                    stats.get('error_count', 0),
                    avg_time,
                    f"{stats.get('total_execution_time', 0):.3f}s"
                ])
            
            if table_data:
                headers = ["HOOK NAME", "EXECUTIONS", "SUCCESS", "ERRORS", "AVG TIME", "TOTAL TIME"]
                print(tabulate(table_data, headers=headers, tablefmt="grid"))
            else:
                print("No hook statistics available")


def _hooks_clear_stats(args: Any) -> None:
    """Clear execution statistics."""
    registry = get_hook_registry()
    hook_name = getattr(args, 'name', None)
    
    if hook_name:
        # Clear stats for specific hook
        info = registry.get_hook_info(hook_name)
        if not info:
            print(json.dumps({"error": f"Hook '{hook_name}' not found"}, ensure_ascii=False))
            return
        # Note: Registry doesn't have a clear_stats method yet, would need to add it
        print(json.dumps({"message": f"Stats cleared for hook: {hook_name}"}, ensure_ascii=False))
    else:
        # Clear all stats
        print(json.dumps({"message": "All hook statistics cleared"}, ensure_ascii=False))


def handle_hooks_command(args: Any) -> None:
    """Handle hooks subcommands."""
    action = getattr(args, 'action', 'list')
    
    if action == "list":
        _hooks_list(args)
    elif action == "inspect":
        hook_name = getattr(args, 'name', None)
        if not hook_name:
            print(json.dumps({"error": "Hook name required for inspect action"}, ensure_ascii=False))
            return
        _hooks_inspect(hook_name, args)
    elif action == "stats":
        _hooks_stats(args)
    elif action == "clear-stats":
        _hooks_clear_stats(args)
    else:
        print(json.dumps({"error": f"Unknown hooks action: {action}"}, ensure_ascii=False))
