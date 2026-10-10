"""The tool servers' status and the tool cache: GET /tools/status, /tools/cache/statistics and
POST /tools/cache/invalidate.

What ToolServerService and the ToolServerIntegration report, read from
agent_system.app_state at call time. Like the debug routes, these answer a
failure with 200 and ``{"error": ...}``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from agent_system import app_state

router = APIRouter()


@router.get("/tools/status")
async def tools_status(force_refresh: bool = False):
    """Get tool server status including plugins and external servers.

    Args:
        force_refresh: If True, invalidates cache before fetching status
    """
    try:
        logger = logging.getLogger(__name__)

        # Use ToolServerService for comprehensive status

        if not app_state.tool_server_service:
            return {"error": "tool server service not initialized"}

        if not app_state.app_registry:
            return {"error": "Registry not initialized"}

        # If force_refresh requested, invalidate cache first
        if force_refresh and app_state.tool_integration:
            try:
                await app_state.tool_integration.invalidate_tools_cache()
                logger.debug("Cache invalidated due to force_refresh=True")
            except Exception as e:
                logger.warning(f"Failed to invalidate cache: {e}")

        # Delegate to ToolServerService
        status = await app_state.tool_server_service.get_comprehensive_status(
            registry=app_state.app_registry,
            check_connectivity=True  # Always check connectivity for accurate status
        )

        return status

    except Exception as e:
        import traceback
        logger = logging.getLogger(__name__)
        logger.error(f"Tool status error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {"error": f"Failed to get tool status: {str(e)}"}


@router.get("/tools/cache/statistics")
async def tools_cache_statistics():
    """Get tool cache statistics for monitoring."""
    try:

        if not app_state.tool_integration:
            return {"error": "tool integration not initialized"}

        # Get cache statistics from tool integration
        stats = await app_state.tool_integration.get_cache_statistics()
        return {
            "success": True,
            "cache": stats
        }

    except Exception as e:
        import traceback
        logger = logging.getLogger(__name__)
        logger.error(f"Tool cache statistics error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {"error": f"Failed to get cache statistics: {str(e)}"}


@router.post("/tools/cache/invalidate")
async def tools_cache_invalidate():
    """Manually invalidate the tool cache."""
    try:

        if not app_state.tool_integration:
            return {"error": "tool integration not initialized"}

        # Invalidate the cache
        await app_state.tool_integration.invalidate_tools_cache()

        return {
            "success": True,
            "message": "Cache invalidated successfully"
        }

    except Exception as e:
        import traceback
        logger = logging.getLogger(__name__)
        logger.error(f"Tool cache invalidation error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {"error": f"Failed to invalidate cache: {str(e)}"}
