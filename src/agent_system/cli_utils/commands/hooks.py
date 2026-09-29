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


def _hooks_inspect(hook_name: str, args: Any) -> bool:
    """Show detailed information about a specific hook."""
    registry = get_hook_registry()
    info = registry.get_hook_info(hook_name)
    
    if not info:
        print(json.dumps({"error": f"Hook '{hook_name}' not found"}, ensure_ascii=False))
        return False
    
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return True


def handle_hooks_command(args: Any) -> bool:
    """Handle hooks subcommands. False means failed: the caller exits 1."""
    action = getattr(args, 'action', 'list')
    
    if action == "list":
        _hooks_list(args)
        return True
    if action == "inspect":
        hook_name = getattr(args, 'name', None)
        if not hook_name:
            print(json.dumps({"error": "Hook name required for inspect action"}, ensure_ascii=False))
            return False
        return _hooks_inspect(hook_name, args)
    print(json.dumps({"error": f"Unknown hooks action: {action}"}, ensure_ascii=False))
    return False
