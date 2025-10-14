"""Context Optimizer Web Endpoints

Provides web UI endpoints for viewing context summarization history and statistics.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.web_adapter import PluginWebInterface

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class ContextOptimizerWebEndpoints(PluginWebInterface):
    """Web endpoints component for context optimizer plugin"""

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig",
        summarization_history: Optional[List[Dict[str, Any]]] = None
    ):
        """Initialize context optimizer web endpoints.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
            summarization_history: Shared list of summarization events
        """
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.summarization_history = summarization_history if summarization_history is not None else []

        # Initialize templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))

        logger.info(f"ContextOptimizerWebEndpoints initialized: {name}")

    def register_routes(self, router: APIRouter, prefix: str = "") -> None:
        """Register web UI routes.

        Args:
            router: FastAPI router instance
            prefix: URL prefix for routes
        """
        @router.get(f"{prefix}/panel", response_class=HTMLResponse)
        async def get_panel(request: Request):
            """Render the context optimizer web UI panel."""
            return self.templates.TemplateResponse(
                "panel.html",
                {
                    "request": request,
                    "plugin_name": self.name,
                    "prefix": prefix
                }
            )

        @router.get(f"{prefix}/history")
        async def get_history(
            session_id: Optional[str] = None,
            limit: int = 100
        ):
            """Get summarization history.

            Args:
                session_id: Filter by session ID (optional)
                limit: Maximum number of entries to return

            Returns:
                List of summarization events with metadata
            """
            history = self.summarization_history

            # Filter by session if requested
            if session_id:
                history = [h for h in history if h.get('session_id') == session_id]

            # Apply limit (most recent first)
            history = list(reversed(history[-limit:]))

            return JSONResponse({
                "success": True,
                "count": len(history),
                "history": history
            })

        @router.get(f"{prefix}/stats")
        async def get_stats():
            """Get overall summarization statistics.

            Returns:
                Aggregated statistics across all sessions
            """
            if not self.summarization_history:
                return JSONResponse({
                    "success": True,
                    "total_summarizations": 0,
                    "total_messages_summarized": 0,
                    "total_tokens_saved": 0,
                    "sessions": []
                })

            # Calculate aggregated stats
            total_summarizations = len(self.summarization_history)
            total_messages_summarized = sum(
                h.get('messages_summarized', 0) for h in self.summarization_history
            )
            total_tokens_saved = sum(
                h.get('tokens_saved', 0) for h in self.summarization_history
            )

            # Per-session stats
            session_stats = {}
            for event in self.summarization_history:
                sid = event.get('session_id', 'unknown')
                if sid not in session_stats:
                    session_stats[sid] = {
                        'session_id': sid,
                        'summarization_count': 0,
                        'messages_summarized': 0,
                        'tokens_saved': 0,
                        'last_summarization': None
                    }

                session_stats[sid]['summarization_count'] += 1
                session_stats[sid]['messages_summarized'] += event.get('messages_summarized', 0)
                session_stats[sid]['tokens_saved'] += event.get('tokens_saved', 0)

                # Track most recent summarization
                timestamp = event.get('timestamp')
                if timestamp:
                    if not session_stats[sid]['last_summarization'] or timestamp > session_stats[sid]['last_summarization']:
                        session_stats[sid]['last_summarization'] = timestamp

            return JSONResponse({
                "success": True,
                "total_summarizations": total_summarizations,
                "total_messages_summarized": total_messages_summarized,
                "total_tokens_saved": total_tokens_saved,
                "sessions": list(session_stats.values())
            })

        @router.delete(f"{prefix}/history")
        async def clear_history():
            """Clear summarization history.

            Returns:
                Success status
            """
            cleared_count = len(self.summarization_history)
            self.summarization_history.clear()

            return JSONResponse({
                "success": True,
                "cleared_count": cleared_count,
                "message": f"Cleared {cleared_count} summarization events"
            })

        logger.info(f"Registered context optimizer web routes with prefix: {prefix}")
