"""Hook Introspection Endpoints: the registered hooks (GET /hooks, /hooks/{hook_name}) and their
execution statistics (/hooks/stats/all, /hooks/stats/{hook_name}).

Read from the hook registry (agent_system.hooks) at call time; a failure answers
200 with ``{"error": ...}``. Administrators only while auth is on, checked here
too and not only by the route rules in config/security.yaml.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from agent_system.api.app_context import errors_as_200
from agent_system.api.debug_endpoints import require_admin_viewer

router = APIRouter(dependencies=[Depends(require_admin_viewer)])


@router.get("/hooks")
@errors_as_200("list hooks", prefixed=False)
async def list_hooks(hook_type: str | None = None):
    """List all registered hooks, optionally filtered by type."""
    from agent_system.hooks import get_hook_registry
    hooks_dict = get_hook_registry().list_hooks()

    if hook_type:
        # Filter by type
        return {hook_type: hooks_dict.get(hook_type, [])}

    return hooks_dict


@router.get("/hooks/{hook_name}")
@errors_as_200("get hook info", prefixed=False)
async def get_hook_info(hook_name: str):
    """Get detailed information about a specific hook."""
    from agent_system.hooks import get_hook_registry
    info = get_hook_registry().get_hook_info(hook_name)

    if not info:
        return {"error": f"Hook '{hook_name}' not found"}

    return info


@router.get("/hooks/stats/all")
@errors_as_200("get hooks stats", prefixed=False)
async def get_all_hooks_stats():
    """Get execution statistics for all hooks."""
    from agent_system.hooks import get_hook_registry
    registry = get_hook_registry()
    all_stats = {}

    for hook_type, hook_names in registry.list_hooks().items():
        for name in hook_names:
            stats = registry.get_stats(name)
            if stats:
                all_stats[name] = stats

    return all_stats


@router.get("/hooks/stats/{hook_name}")
@errors_as_200("get hook stats", prefixed=False)
async def get_hook_stats(hook_name: str):
    """Get execution statistics for a specific hook."""
    from agent_system.hooks import get_hook_registry
    registry = get_hook_registry()
    stats = registry.get_stats(hook_name)

    if stats is None:
        info = registry.get_hook_info(hook_name)
        if not info:
            return {"error": f"Hook '{hook_name}' not found"}
        return {hook_name: {}}

    return {hook_name: stats}
