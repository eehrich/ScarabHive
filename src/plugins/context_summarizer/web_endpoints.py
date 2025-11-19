"""Web UI endpoints for Context Summarizer plugin.

Provides REST API endpoints for viewing summarization history and statistics.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


class ContextSummarizerWebFactory:
    """Web UI factory for Context Summarizer plugin.

    Provides panel for viewing LLM-based summarization history with
    before/after message comparison and statistics.
    """

    def __init__(
        self,
        server,  # ContextSummarizerServer instance
        summarization_history: List[Dict[str, Any]]
    ):
        """Initialize web factory.

        Args:
            server: ContextSummarizerServer instance (provides schema, name, etc.)
            summarization_history: Shared list for tracking summarization events
        """
        self.server = server
        self.name = server.name if server else "context_summarizer"
        self.summarization_history = summarization_history
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))

    def get_web_router(self) -> APIRouter:
        """Create router from schema definition."""
        from agent_system.plugins.schema_router import create_schema_router
        schema = self.server.get_schema_data() if self.server and hasattr(self.server, 'get_schema_data') else {}
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self
        )

    async def get_history(self, limit: int = 100) -> Dict[str, Any]:
        """Get recent summarization events.

        Args:
            limit: Maximum number of events to return

        Returns:
            Dict with summarization history
        """
        try:
            # Return most recent events first
            recent_events = list(reversed(self.summarization_history[-limit:]))

            return {
                'success': True,
                'events': recent_events,
                'total_events': len(self.summarization_history)
            }
        except Exception as e:
            logger.exception("Error getting summarization history")
            return {
                'success': False,
                'error': str(e),
                'events': []
            }

    async def get_stats(self) -> Dict[str, Any]:
        """Get summarization statistics.

        Returns:
            Dict with aggregate statistics
        """
        try:
            if not self.summarization_history:
                return {
                    'success': True,
                    'total_events': 0,
                    'total_tokens_saved': 0,
                    'total_messages_summarized': 0,
                    'average_reduction_ratio': 0.0
                }

            total_tokens_saved = sum(event.get('tokens_saved', 0) for event in self.summarization_history)
            total_messages_summarized = sum(event.get('messages_summarized', 0) for event in self.summarization_history)
            avg_reduction = sum(event.get('reduction_ratio', 0) for event in self.summarization_history) / len(self.summarization_history)

            return {
                'success': True,
                'total_events': len(self.summarization_history),
                'total_tokens_saved': total_tokens_saved,
                'total_messages_summarized': total_messages_summarized,
                'average_reduction_ratio': avg_reduction
            }
        except Exception as e:
            logger.exception("Error calculating summarization stats")
            return {
                'success': False,
                'error': str(e)
            }

    async def render_panel(self, request: Request) -> HTMLResponse:
        """Render the summarization history panel."""
        return self.templates.TemplateResponse(
            request,
            "panel.html",
            {
                "plugin_name": self.name,
                "panel_title": "Context Summarization History"
            }
        )
