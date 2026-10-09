"""Hook Introspection Endpoints: the registered hooks (GET /hooks, /hooks/{hook_name}) and their
execution statistics (/hooks/stats/all, /hooks/stats/{hook_name}).

Read from the hook registry (agent_system.hooks) at call time; a failure answers
200 with ``{"error": ...}``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

router = APIRouter()


@router.get("/hooks")
async def list_hooks(hook_type: str | None = None):
    """List all registered hooks, optionally filtered by type."""
    try:
        from agent_system.hooks import get_hook_registry
        registry = get_hook_registry()
        hooks_dict = registry.list_hooks()

        if hook_type:
            # Filter by type
            return {hook_type: hooks_dict.get(hook_type, [])}

        return hooks_dict
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.exception("Failed to list hooks: %s", e)
        return {"error": str(e)}


@router.get("/hooks/{hook_name}")
async def get_hook_info(hook_name: str):
    """Get detailed information about a specific hook."""
    try:
        from agent_system.hooks import get_hook_registry
        registry = get_hook_registry()
        info = registry.get_hook_info(hook_name)

        if not info:
            return {"error": f"Hook '{hook_name}' not found"}

        return info
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.exception("Failed to get hook info: %s", e)
        return {"error": str(e)}


@router.get("/hooks/stats/all")
async def get_all_hooks_stats():
    """Get execution statistics for all hooks."""
    try:
        from agent_system.hooks import get_hook_registry
        registry = get_hook_registry()
        hooks_dict = registry.list_hooks()
        all_stats = {}

        for hook_type, hook_names in hooks_dict.items():
            for name in hook_names:
                stats = registry.get_stats(name)
                if stats:
                    all_stats[name] = stats

        return all_stats
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.exception("Failed to get hooks stats: %s", e)
        return {"error": str(e)}


@router.get("/hooks/stats/{hook_name}")
async def get_hook_stats(hook_name: str):
    """Get execution statistics for a specific hook."""
    try:
        from agent_system.hooks import get_hook_registry
        registry = get_hook_registry()
        stats = registry.get_stats(hook_name)

        if stats is None:
            info = registry.get_hook_info(hook_name)
            if not info:
                return {"error": f"Hook '{hook_name}' not found"}
            return {hook_name: {}}

        return {hook_name: stats}
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.exception("Failed to get hook stats: %s", e)
        return {"error": str(e)}
