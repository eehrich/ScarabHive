"""Web UI endpoints for Context Engineer plugin.

Provides REST API endpoints for viewing context engineering statistics
and history, plus an HTML panel for the web interface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


class ContextEngineerWebFactory:
    """Web UI factory for Context Engineer plugin.
    
    Provides panel for viewing context engineering statistics including:
    - Compaction history (tokens saved, layers applied)
    - Core memory facts
    - Stored variables
    - Archived messages
    """
    
    def __init__(
        self,
        server,  # ContextEngineerServer instance
        stats_history: list[dict[str, Any]]
    ):
        """Initialize web factory.
        
        Args:
            server: ContextEngineerServer instance
            stats_history: Shared list for tracking compaction events
        """
        self.server = server
        self.name = server.name if server else "context_engineer"
        self.stats_history = stats_history
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))
    
    def get_web_router(self) -> APIRouter:
        """Create router from schema definition."""
        from agent_system.plugins.schema_router import create_schema_router
        
        schema = self.server.get_schema_data() if self.server and hasattr(self.server, "get_schema_data") else {}
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self
        )
    
    async def get_history(self, request: Request) -> dict[str, Any]:
        """Get recent compaction events.
        
        Args:
            request: FastAPI request object
            
        Returns:
            Dict with compaction history
        """
        limit = int(request.query_params.get("limit", 100))
        
        try:
            # Return most recent events first
            recent_events = list(reversed(self.stats_history[-limit:]))
            
            return {
                "success": True,
                "events": recent_events,
                "total_events": len(self.stats_history)
            }
        except Exception as e:
            logger.exception("Error getting compaction history")
            return {
                "success": False,
                "error": str(e),
                "events": []
            }
    
    async def get_stats(self, request: Request) -> dict[str, Any]:
        """Get aggregate statistics.
        
        Args:
            request: FastAPI request object
            
        Returns:
            Dict with aggregate statistics
        """
        try:
            if not self.stats_history:
                return {
                    "success": True,
                    "total_events": 0,
                    "total_tokens_saved": 0,
                    "total_tool_results_stored": 0,
                    "total_variables_created": 0,
                    "total_messages_archived": 0,
                    "total_media_deduplicated": 0,
                    "total_media_compacted": 0,
                    "average_reduction_percent": 0.0
                }
            
            total_tokens_saved = sum(
                event.get("tokens_saved", 0) for event in self.stats_history
            )
            total_tool_results = sum(
                event.get("tool_results_stored", 0) for event in self.stats_history
            )
            total_variables = sum(
                event.get("variables_created", 0) for event in self.stats_history
            )
            total_archived = sum(
                event.get("messages_archived", 0) for event in self.stats_history
            )
            total_media_deduplicated = sum(
                event.get("media_deduplicated", 0) for event in self.stats_history
            )
            total_media_compacted = sum(
                event.get("media_compacted_after_event", 0) for event in self.stats_history
            )
            avg_reduction = sum(
                event.get("reduction_percent", 0) for event in self.stats_history
            ) / len(self.stats_history)
            
            return {
                "success": True,
                "total_events": len(self.stats_history),
                "total_tokens_saved": total_tokens_saved,
                "total_tool_results_stored": total_tool_results,
                "total_variables_created": total_variables,
                "total_messages_archived": total_archived,
                "total_media_deduplicated": total_media_deduplicated,
                "total_media_compacted": total_media_compacted,
                "average_reduction_percent": round(avg_reduction, 1)
            }
        except Exception as e:
            logger.exception("Error calculating stats")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def get_session_details(self, request: Request) -> dict[str, Any]:
        """Get detailed stats for a specific session.
        
        Args:
            request: FastAPI request object with session_id query param
            
        Returns:
            Dict with session-specific statistics
        """
        session_id = request.query_params.get("session_id", "default")
        
        try:
            # Get session stats from hooks implementation
            if hasattr(self.server, "_hooks_impl"):
                result = await self.server._hooks_impl._handle_stats(session_id)
                return {
                    "success": True,
                    "session_id": session_id,
                    **result
                }
            else:
                return {
                    "success": False,
                    "error": "Server not initialized"
                }
        except Exception as e:
            logger.exception("Error getting session details")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def get_panel(self, request: Request) -> HTMLResponse:
        """Render the context engineer panel."""
        return self.templates.TemplateResponse(
            "panel.html",
            {
                "request": request,
                "plugin_name": self.name
            }
        )
